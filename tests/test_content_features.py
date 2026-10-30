"""正文读取、正文证据、印章识别的单元测试。

测试用 PDF 由 pymupdf 现场生成（带文字层），不依赖样例目录，也不需要 OCR。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from tests.helpers import make_checkpoint, make_file  # noqa: F401  (设置 sys.path)

from core.content import (
    DEEP_MATCH,
    enrich_with_content,
    requires_seal,
    score_content,
)
from core.models import STATE_MATCHED, STATE_MISSING, STATE_SUSPECT, Evidence, MatchResult
from core.pdf_reader import ContentReader
from core.seal import detect_seals, scan_seals
from core.text import clean_for_match

import cv2
import pymupdf


def _make_pdf(path: Path, pages: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 100), text, fontname="china-s", fontsize=14)
    doc.save(str(path))
    doc.close()
    return path


def _seal_image(kind: str = "ring", size: int = 800) -> np.ndarray:
    image = np.full((size, size, 3), 255, np.uint8)
    red = (200, 30, 40)
    if kind == "ring":
        cv2.circle(image, (size // 2, size // 2), 95, red, 6)
        cv2.putText(image, "S", (size // 2 - 18, size // 2 + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, red, 4)
    elif kind == "rect":
        cv2.rectangle(image, (250, 340), (550, 460), red, 6)
    elif kind == "gray":
        gray = np.full((size, size), 240, np.uint8)
        cv2.circle(gray, (size // 2, size // 2), 95, 40, 6)
        image = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    return image


class SealTests(unittest.TestCase):
    def test_round_seal_detected(self):
        boxes = detect_seals(_seal_image("ring"), page=1, dpi=120)
        self.assertEqual(len(boxes), 1)
        self.assertGreater(boxes[0].w, 150)

    def test_rectangular_table_frame_rejected(self):
        # 档案页眉的红色“档号/序号”表格框不是印章
        boxes = detect_seals(_seal_image("rect"), page=1, dpi=120)
        self.assertEqual(boxes, [])

    def test_grayscale_page_is_undecidable(self):
        scan = scan_seals([(1, _seal_image("gray"))], dpi=120)
        self.assertEqual(scan.state, "未知")

    def test_blank_page_has_no_seal(self):
        blank = np.full((800, 800, 3), 255, np.uint8)
        scan = scan_seals([(1, blank)], dpi=120)
        self.assertEqual(scan.state, "未知")  # 全白页也算不可判


class ContentScoreTests(unittest.TestCase):
    def test_matching_text_scores_high(self):
        cp = make_checkpoint("水泥出厂合格证及检验报告")
        score, hits = score_content(
            cp, clean_for_match("水泥出厂合格证 检验报告 施工单位 广东火电")
        )
        self.assertGreaterEqual(score, 0.8)
        self.assertIn("水泥", "".join(hits))

    def test_unrelated_text_scores_low(self):
        cp = make_checkpoint("水泥出厂合格证及检验报告")
        score, _ = score_content(cp, clean_for_match("钢筋进场见证取样记录表"))
        self.assertLess(score, 0.25)

    def test_requires_seal(self):
        self.assertTrue(requires_seal(make_checkpoint("开工报审表")))
        self.assertTrue(requires_seal(make_checkpoint("隐蔽工程签证")))
        self.assertFalse(requires_seal(make_checkpoint("项目部成立文件")))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_text_layer_stops_early(self):
        path = _make_pdf(self.root / "a.pdf", ["第一页内容", "第二页内容", "第三页内容"])
        reader = ContentReader(use_ocr=False, max_pages=3, check_seal=False)
        doc = reader.read(path, stop_when=lambda text, page: page >= 1)
        self.assertTrue(doc.stopped_early)
        self.assertEqual(doc.read_pages, 1)
        self.assertIn("第一页", doc.text)

    def test_reads_all_pages_without_stop(self):
        path = _make_pdf(self.root / "b.pdf", ["一", "二", "三"])
        reader = ContentReader(use_ocr=False, max_pages=5, check_seal=False)
        doc = reader.read(path)
        self.assertFalse(doc.stopped_early)
        self.assertEqual(doc.read_pages, 3)

    def test_cache_reused_on_second_run(self):
        path = _make_pdf(self.root / "c.pdf", ["内容"])
        cache = self.root / "cache.json"
        first = ContentReader(use_ocr=False, check_seal=False, cache_path=cache)
        first.read(path)
        first.save()
        second = ContentReader(use_ocr=False, check_seal=False, cache_path=cache)
        doc = second.read(path)
        self.assertEqual(second.hits, 1)
        self.assertIn("内容", doc.text)

    def test_missing_file_reports_error(self):
        reader = ContentReader(use_ocr=False, check_seal=False)
        doc = reader.read(self.root / "nope.pdf")
        self.assertTrue(doc.error)


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _reader(self):
        return ContentReader(use_ocr=False, max_pages=3, check_seal=False)

    def test_suspect_promoted_by_content(self):
        _make_pdf(self.root / "施工" / "1.pdf", ["水泥出厂合格证及检验报告 广东火电"])
        cp = make_checkpoint("水泥出厂合格证及检验报告")
        file = make_file("施工/1.pdf")
        result = MatchResult(
            checkpoint=cp, state=STATE_SUSPECT, score=0.45,
            evidence=[Evidence(rel_path="施工/1.pdf", score=0.45)],
        )
        stats = enrich_with_content([result], [file], self.root, self._reader())
        self.assertEqual(result.state, STATE_MATCHED)
        self.assertEqual(stats.promoted, 1)
        self.assertGreaterEqual(result.content_score, 0.55)
        self.assertIn("正文命中", result.reason)

    def test_weak_content_does_not_downgrade(self):
        _make_pdf(self.root / "施工" / "2.pdf", ["钢筋原材复试报告"])
        cp = make_checkpoint("水泥出厂合格证及检验报告")
        file = make_file("施工/2.pdf")
        result = MatchResult(
            checkpoint=cp, state=STATE_MATCHED, score=0.70,
            evidence=[Evidence(rel_path="施工/2.pdf", score=0.70)],
        )
        enrich_with_content([result], [file], self.root, self._reader())
        self.assertEqual(result.state, STATE_MATCHED)
        self.assertIn("建议复核", result.reason)

    def test_deep_mode_rescues_missing(self):
        text = "水泥出厂合格证及检验报告 施工单位 广东火电"
        _make_pdf(self.root / "扫描件" / "001.pdf", [text])
        cp = make_checkpoint("水泥出厂合格证及检验报告")
        file = make_file("扫描件/001.pdf")
        file.best_score = 0.05
        result = MatchResult(checkpoint=cp, state=STATE_MISSING, score=0.0)
        stats = enrich_with_content(
            [result], [file], self.root, self._reader(), deep=True, deep_max_files=5
        )
        self.assertEqual(result.state, STATE_SUSPECT)
        self.assertEqual(stats.rescued, 1)
        self.assertEqual(result.evidence[0].rel_path, "扫描件/001.pdf")
        self.assertGreaterEqual(result.content_score, DEEP_MATCH)

    def test_weak_warning_suppressed_when_subject_is_synonym(self):
        # 清单写“杆塔”，现场写“铁塔组立”，主语对得上，不该提示“正文未见清单字样”
        _make_pdf(self.root / "施工" / "4.pdf", ["铁塔组立分部工程开工报告及报审表 施工单位"])
        cp = make_checkpoint("杆塔分部工程开工及报审表")
        file = make_file("施工/4.pdf")
        result = MatchResult(
            checkpoint=cp, state=STATE_MATCHED, score=0.70,
            evidence=[Evidence(rel_path="施工/4.pdf", score=0.70)],
        )
        enrich_with_content([result], [file], self.root, self._reader())
        self.assertNotIn("建议复核", result.reason)

    def test_no_weak_warning_from_secondary_evidence(self):
        # 第 1 候选文件没读到正文，不能拿第 2 候选的低分去说主证据对不上
        _make_pdf(self.root / "施工" / "5b.pdf", ["路径复测记录表 与清单无关"])
        cp = make_checkpoint("水泥出厂合格证及检验报告")
        file = make_file("施工/5b.pdf")
        result = MatchResult(
            checkpoint=cp, state=STATE_MATCHED, score=0.70,
            evidence=[
                Evidence(rel_path="施工/5a.pdf", score=0.70),
                Evidence(rel_path="施工/5b.pdf", score=0.44),
            ],
        )
        enrich_with_content([result], [file], self.root, self._reader())
        self.assertNotIn("建议复核", result.reason)

    def test_ambiguous_group_needs_distinguishing_word(self):
        # 四条“见证取样记录”靠归档内容区分；一份商品混凝土取样记录不能把钢筋那条也升上去
        _make_pdf(self.root / "施工" / "06商品混凝土见证取样记录.pdf",
                  ["见证取样送检记录表 混凝土试块"])
        concrete = make_checkpoint("见证取样记录", archive="06商品混凝土", seq=1)
        rebar = make_checkpoint("见证取样记录", archive="07钢筋", seq=2)
        file = make_file("施工/06商品混凝土见证取样记录.pdf")
        results = [
            MatchResult(checkpoint=cp, state=STATE_SUSPECT, score=0.45,
                        evidence=[Evidence(rel_path="施工/06商品混凝土见证取样记录.pdf",
                                           score=0.45)])
            for cp in (concrete, rebar)
        ]
        stats = enrich_with_content(results, [file], self.root, self._reader())
        self.assertEqual(results[0].state, STATE_MATCHED)
        self.assertEqual(results[1].state, STATE_SUSPECT)
        self.assertEqual(stats.promoted, 1)
        self.assertEqual(stats.gated, 1)

    def test_ambiguous_group_allows_filename_distinguishing_word(self):
        # 正文里没有“钢筋”，但文件名写明钢筋，仍然可以升
        _make_pdf(self.root / "施工" / "07钢筋见证取样记录.pdf",
                  ["见证取样送检记录表 混凝土试块"])
        concrete = make_checkpoint("见证取样记录", archive="06商品混凝土", seq=1)
        rebar = make_checkpoint("见证取样记录", archive="07钢筋", seq=2)
        file = make_file("施工/07钢筋见证取样记录.pdf")
        results = [
            MatchResult(checkpoint=cp, state=STATE_SUSPECT, score=0.45,
                        evidence=[Evidence(rel_path="施工/07钢筋见证取样记录.pdf", score=0.45)])
            for cp in (concrete, rebar)
        ]
        enrich_with_content(results, [file], self.root, self._reader())
        self.assertEqual(results[1].state, STATE_MATCHED)
        self.assertEqual(results[0].state, STATE_SUSPECT)

    def test_seal_state_attached_for_seal_documents(self):
        _make_pdf(self.root / "施工" / "3.pdf", ["开工报审表 施工单位"])
        cp = make_checkpoint("开工报审表")
        file = make_file("施工/3.pdf")
        result = MatchResult(
            checkpoint=cp, state=STATE_MATCHED, score=0.70,
            evidence=[Evidence(rel_path="施工/3.pdf", score=0.70)],
        )
        reader = ContentReader(use_ocr=False, max_pages=1, check_seal=True)
        enrich_with_content([result], [file], self.root, reader)
        self.assertTrue(result.needs_seal)
        # 生成的 PDF 是纯文字、白底，没有红色印章
        self.assertEqual(result.seal_state, "未知")


if __name__ == "__main__":
    unittest.main()
