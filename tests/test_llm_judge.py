"""大模型复判模块的离线测试：全部用假客户端，不联网、不需要 key。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  让 core 可导入

from core.llm_judge import (
    JudgeRequest,
    LLMConfig,
    LLMJudge,
    LLMVerdict,
    apply_verdict,
    build_prompt,
    build_shortlist_prompt,
    case_index,
    drawing_title,
    duty_sources,
    estimate_tokens,
    excerpt_for,
    judge_request,
    nearby_names,
    override_entry,
    parse_shortlist,
    parse_verdict,
)
from core.models import Checkpoint, Evidence, MatchResult

EXCERPT = "基础工程质量验收记录。本表为8231-03卷，验收结论：合格。单位工程：线路工程。"


def make_result(
    checkpoint_id="8231-03",
    text="基础工程质量验收记录",
    evidence=(("施工/8231 基础工程/2.基础工程质量验收记录.pdf", 0.58),),
) -> MatchResult:
    checkpoint = Checkpoint(
        id=checkpoint_id,
        node_key="8-82-8231",
        code="8231",
        category="基础工程",
        path=["8 基本建设", "82 交流输电线路", "8231 基础工程"],
        archive_content="基础工程质量验收记录",
        text=text,
        seq=1,
        source="归档范围表",
        filing="施工",
        retention={"施工": "永久"},
    )
    result = MatchResult(checkpoint=checkpoint, state="疑似", score=0.58)
    result.evidence = [Evidence(rel_path=path, score=score) for path, score in evidence]
    return result


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)[:300]

    def json(self):
        return self._payload


def openai_body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class FakeSession:
    """按顺序返回预置响应；记录每次调用。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002 - 对齐 requests 签名
        self.calls.append({"url": url, "headers": headers or {}, "body": json or {}})
        response = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def judge_with(session, **kwargs) -> LLMJudge:
    config = LLMConfig(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key="sk-test",
        retries=kwargs.pop("retries", 3),
        workers=kwargs.pop("workers", 2),
    )
    return LLMJudge(config, session=session, sleep=lambda _s: None, **kwargs)


class BuildPromptTest(unittest.TestCase):
    def test_prompt_carries_checkpoint_evidence_excerpt(self):
        prompt = build_prompt(make_result(), EXCERPT, nearby=["3.竣工图.pdf"], gate="基础")
        self.assertIn("8231", prompt)
        self.assertIn("基础工程质量验收记录", prompt)
        self.assertIn("2.基础工程质量验收记录.pdf", prompt)
        self.assertIn("3.竣工图.pdf", prompt)
        self.assertIn("验收结论：合格", prompt)
        self.assertIn("区分词", prompt)

    def test_missing_item_says_no_candidate(self):
        result = make_result(evidence=())
        prompt = build_prompt(result)
        self.assertIn("未检索到候选文件", prompt)
        self.assertIn("未读到正文", prompt)


class NearbyNamesTest(unittest.TestCase):
    def test_filters_by_code_prefix(self):
        class Rec:
            def __init__(self, rel):
                self.rel_path = rel

        files = [
            Rec("施工/8231 基础工程/1.开工报告.pdf"),
            Rec("施工/8232 杆塔工程/2.组立记录.pdf"),
            Rec("施工/8231 基础工程/3.竣工图.pdf"),
        ]
        names = nearby_names("8231-03", files)
        self.assertEqual(names, ["1.开工报告.pdf", "3.竣工图.pdf"])


class ParseVerdictTest(unittest.TestCase):
    def test_plain_json(self):
        raw = json.dumps({"verdict": "match", "confidence": 0.8,
                          "reason": "并卷", "quote": "验收结论：合格"}, ensure_ascii=False)
        verdict = parse_verdict(raw, EXCERPT)
        self.assertEqual(verdict.verdict, "match")
        self.assertAlmostEqual(verdict.confidence, 0.8)
        self.assertTrue(verdict.ok)

    def test_code_fence_and_chatter(self):
        raw = '好的，结论如下：\n```json\n{"verdict":"gap","confidence":0.6,"reason":"没有",' \
              '"quote":"基础工程质量验收记录"}\n```\n以上。'
        verdict = parse_verdict(raw, EXCERPT)
        self.assertEqual(verdict.verdict, "gap")

    def test_quote_from_filename_is_allowed(self):
        raw = json.dumps({"verdict": "match", "confidence": 0.7, "reason": "异名",
                          "quote": "2.基础工程质量验收记录.pdf"}, ensure_ascii=False)
        verdict = parse_verdict(raw, "", names=["2.基础工程质量验收记录.pdf"])
        self.assertEqual(verdict.verdict, "match")

    def test_quote_not_in_source_downgrades(self):
        raw = json.dumps({"verdict": "match", "confidence": 0.9, "reason": "页十五有盖章",
                          "quote": "第十五页加盖了项目部公章"}, ensure_ascii=False)
        verdict = parse_verdict(raw, EXCERPT)
        self.assertEqual(verdict.verdict, "unclear")
        self.assertIn("quote", verdict.error)

    def test_quote_punctuation_insensitive(self):
        raw = json.dumps({"verdict": "match", "confidence": 0.5, "reason": "x",
                          "quote": "验收结论 合格"}, ensure_ascii=False)
        self.assertEqual(parse_verdict(raw, EXCERPT).verdict, "match")

    def test_gap_without_quote_is_allowed(self):
        # 「真缺」是断言不存在，没有可引用的原文，不该因此降级
        raw = json.dumps({"verdict": "gap", "confidence": 0.7, "reason": "全目录无此文件",
                          "quote": ""}, ensure_ascii=False)
        self.assertEqual(parse_verdict(raw, EXCERPT).verdict, "gap")

    def test_gap_with_bogus_quote_downgrades(self):
        raw = json.dumps({"verdict": "gap", "confidence": 0.7, "reason": "x",
                          "quote": "这句话不在任何材料里"}, ensure_ascii=False)
        self.assertEqual(parse_verdict(raw, EXCERPT).verdict, "unclear")

    def test_bad_verdict_value(self):
        raw = json.dumps({"verdict": "probably", "reason": "x", "quote": ""}, ensure_ascii=False)
        self.assertEqual(parse_verdict(raw, EXCERPT).verdict, "unclear")

    def test_non_json(self):
        verdict = parse_verdict("我觉得应该算已提供", EXCERPT)
        self.assertEqual(verdict.verdict, "unclear")
        self.assertTrue(verdict.error)

    def test_empty(self):
        self.assertEqual(parse_verdict("", EXCERPT).verdict, "unclear")


class JudgeTest(unittest.TestCase):
    def test_success_and_cache(self):
        body = openai_body(json.dumps({"verdict": "match", "confidence": 0.75, "reason": "并卷",
                                       "quote": "验收结论：合格"}, ensure_ascii=False))
        session = FakeSession(FakeResponse(body))
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "llm_cache.json"
            judge = judge_with(session, cache_path=cache)
            first = judge.judge("prompt-A", EXCERPT)
            second = judge.judge("prompt-A", EXCERPT)
            judge.save()
            self.assertEqual(first.verdict, "match")
            self.assertFalse(first.cached)
            self.assertTrue(second.cached)
            self.assertEqual(len(session.calls), 1)  # 第二次走缓存
            # 新建实例也能命中缓存
            again = judge_with(session, cache_path=cache).judge("prompt-A", EXCERPT)
            self.assertTrue(again.cached)
            self.assertEqual(len(session.calls), 1)

    def test_retry_then_success(self):
        ok_body = openai_body(json.dumps({"verdict": "gap", "confidence": 0.6, "reason": "缺",
                                          "quote": "基础工程质量验收记录"}, ensure_ascii=False))
        session = FakeSession(FakeResponse({"error": "boom"}, status_code=500), FakeResponse(ok_body))
        verdict = judge_with(session).judge("p", EXCERPT)
        self.assertEqual(verdict.verdict, "gap")
        self.assertEqual(len(session.calls), 2)

    def test_auth_error_not_retried(self):
        session = FakeSession(FakeResponse({"error": "bad key"}, status_code=401))
        judge = judge_with(session)
        verdict = judge.judge("p", EXCERPT)
        self.assertEqual(verdict.verdict, "unclear")
        self.assertIn("401", verdict.error)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(judge.errors, 1)

    def test_network_error_exhausts_retries(self):
        session = FakeSession(RuntimeError("timeout"))
        judge = judge_with(session, retries=2)
        verdict = judge.judge("p", EXCERPT)
        self.assertEqual(verdict.verdict, "unclear")
        self.assertEqual(len(session.calls), 2)

    def test_judge_many(self):
        bodies = [FakeResponse(openai_body(json.dumps(
            {"verdict": "match", "confidence": 0.7, "reason": "a", "quote": "验收结论：合格"},
            ensure_ascii=False)))] * 3
        session = FakeSession(*bodies)
        judge = judge_with(session)
        items = [(str(i), f"prompt-{i}", EXCERPT, ()) for i in range(3)]
        out = judge.judge_many(items)
        self.assertEqual(set(out), {"0", "1", "2"})
        self.assertTrue(all(v.verdict == "match" for v in out.values()))
        self.assertEqual(len(session.calls), 3)

    def test_anthropic_style_request(self):
        session = FakeSession(FakeResponse({"content": [{"type": "text", "text": json.dumps(
            {"verdict": "gap", "confidence": 0.5, "reason": "缺", "quote": "基础工程质量验收记录"},
            ensure_ascii=False)}]}))
        config = LLMConfig(base_url="https://api.anthropic.com/v1", model="claude-x",
                           api_key="sk-ant", style="anthropic")
        judge = LLMJudge(config, session=session, sleep=lambda _s: None)
        verdict = judge.judge("p", EXCERPT)
        self.assertEqual(verdict.verdict, "gap")
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/messages"))
        self.assertEqual(call["headers"].get("x-api-key"), "sk-ant")
        self.assertIn("system", call["body"])


class ConfigTest(unittest.TestCase):
    def test_from_env(self):
        import os

        saved = {k: os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")}
        os.environ.update({
            "LLM_BASE_URL": "https://api.deepseek.com/v1",
            "LLM_MODEL": "deepseek-chat",
            "LLM_API_KEY": "sk-xyz",
        })
        try:
            config = LLMConfig.from_env()
            self.assertTrue(config.ready)
            self.assertEqual(config.model, "deepseek-chat")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_not_ready(self):
        config = LLMConfig(base_url="", model="", api_key="")
        self.assertFalse(config.ready)
        self.assertIn("地址", config.missing)


class ApplyVerdictTest(unittest.TestCase):
    def test_fields_and_text(self):
        result = make_result()
        self.assertEqual(result.llm_text, "未复判")
        apply_verdict(result, LLMVerdict(verdict="match", confidence=0.8, reason="并卷",
                                         quote="验收结论：合格", cached=True))
        self.assertEqual(result.llm_verdict, "match")
        self.assertEqual(result.llm_text, "建议已提供（0.80）·缓存")
        self.assertEqual(result.state, "疑似")  # 复判不改判定

    def test_estimate_tokens(self):
        self.assertGreater(estimate_tokens(["a" * 160]), 50)


class OverrideEntryTest(unittest.TestCase):
    def test_match_becomes_matched(self):
        result = make_result()
        apply_verdict(result, LLMVerdict(verdict="match", confidence=0.8,
                                         reason="异名", quote="2.基础工程质量验收记录.pdf"))
        entry = override_entry(result, "张三")
        self.assertEqual(entry["state"], "已匹配")
        self.assertIn("采纳大模型建议", entry["note"])
        self.assertIn("异名", entry["note"])
        self.assertEqual(entry["reviewer"], "张三")

    def test_gap_becomes_missing(self):
        result = make_result()
        apply_verdict(result, LLMVerdict(verdict="gap", confidence=0.7, reason="缺"))
        self.assertEqual(override_entry(result)["state"], "缺失")

    def test_unclear_does_not_override(self):
        result = make_result()
        apply_verdict(result, LLMVerdict(verdict="unclear", confidence=0.2, reason="看不懂"))
        self.assertIsNone(override_entry(result))


class DutyBasisTest(unittest.TestCase):
    """按「归档责任」判已提供时，引用的是清单字段，走 duty 这一套源。"""

    DUTY = ["建设 / 施工", "立卷单位：建设 / 施工", "地方政府建设", "清单来源：地方政府建设"]

    def verdict(self, payload, **kwargs):
        return parse_verdict(json.dumps(payload, ensure_ascii=False), duty=self.DUTY, **kwargs)

    def test_duty_quote_accepted(self):
        v = self.verdict({"verdict": "match", "confidence": 0.8, "reason": "由建设单位归档",
                          "basis": "duty", "quote": "建设 / 施工"})
        self.assertEqual(v.verdict, "match")

    def test_label_prefixed_quote_still_matches(self):
        v = self.verdict({"verdict": "match", "confidence": 0.8, "reason": "地方政府出具",
                          "basis": "duty", "quote": "清单来源：地方政府建设"})
        self.assertEqual(v.verdict, "match")

    def test_duty_basis_cannot_quote_a_file(self):
        v = self.verdict({"verdict": "match", "confidence": 0.8, "reason": "并卷",
                          "basis": "duty", "quote": "2.基础工程质量验收记录.pdf"},
                         excerpt=EXCERPT, names=["2.基础工程质量验收记录.pdf"])
        self.assertEqual(v.verdict, "unclear")

    def test_file_basis_cannot_quote_duty(self):
        v = self.verdict({"verdict": "match", "confidence": 0.8, "reason": "对方归档",
                          "basis": "file", "quote": "建设 / 施工"})
        self.assertEqual(v.verdict, "unclear")

    def test_missing_basis_accepts_either_source(self):
        v = self.verdict({"verdict": "match", "confidence": 0.8, "reason": "对方归档",
                          "quote": "建设 / 施工"})
        self.assertEqual(v.verdict, "match")

    def test_construction_only_duty_cannot_prove_provided(self):
        # 「立卷单位：施工」恰恰说明该由自己交，拿它证明已提供是自相矛盾
        v = parse_verdict(
            json.dumps({"verdict": "match", "confidence": 0.9, "reason": "责任在施工",
                        "basis": "duty", "quote": "立卷单位：施工"}, ensure_ascii=False),
            duty=["施工", "立卷单位：施工", "相关", "清单来源：相关"],
        )
        self.assertEqual(v.verdict, "unclear")
        self.assertIn("归档责任", v.error)

    def test_file_basis_still_works_when_only_construction(self):
        v = parse_verdict(
            json.dumps({"verdict": "match", "confidence": 0.8, "reason": "异名",
                        "basis": "file", "quote": "2.基础工程质量验收记录.pdf"},
                       ensure_ascii=False),
            excerpt=EXCERPT, names=["2.基础工程质量验收记录.pdf"],
            duty=["施工", "立卷单位：施工"],
        )
        self.assertEqual(v.verdict, "match")

    def test_duty_sources_include_labeled_lines(self):
        sources = duty_sources(make_result())
        self.assertIn("施工", sources)
        self.assertIn("立卷单位：施工", sources)


class DrawingTitleTest(unittest.TestCase):
    """图纸的题目在图签上，正文前几百字全是图面注记。"""

    def test_title_block_with_stamp(self):
        text = "塔号 A1\n角度 15\n出图专用章\n平断面图3（1A11A~2A1）"
        self.assertEqual(drawing_title(text), "平断面图3（1A11A~2A1）")

    def test_title_field_line(self):
        self.assertEqual(drawing_title("说明\n图名：一般钢结构塔组装图"), "一般钢结构塔组装图")

    def test_tail_line_without_stamp(self):
        self.assertEqual(drawing_title("平面布置图1（A1~A9）"), "平面布置图1（A1~A9）")

    def test_plain_document_has_no_title(self):
        self.assertEqual(drawing_title(EXCERPT), "")

    def test_excerpt_puts_title_first(self):
        filler = "塔号 A1 转角度 15 度\n" * 60
        text = filler + "平断面图12（1A1~2A1）"
        excerpt = excerpt_for(text, 800)
        self.assertTrue(excerpt.startswith("图纸标题（取自图签）：平断面图12（1A1~2A1）"))
        self.assertLessEqual(len(excerpt), 800 + 40)

    def test_plain_excerpt_unchanged(self):
        self.assertEqual(excerpt_for(EXCERPT), EXCERPT)


class ShortlistTest(unittest.TestCase):
    def test_prompt_lists_cases_and_asks(self):
        prompt = build_shortlist_prompt(make_result(), [("8231 基础工程", 12), ("8230 成立文件", 16)])
        self.assertIn("8231 基础工程（12）", prompt)
        self.assertIn('"cases"', prompt)
        self.assertIn("主要项目文件", prompt)

    def test_parse_exact_and_abbreviated(self):
        labels = ["8230-10 设计变更文件", "8230-11 设计变更文件（二）", "8241 杆塔"]
        raw = '{"cases":["8241","8230-10"],"reason":"图纸类"}'
        # 返回的是真实案卷名（后面要拿它去取卷内文件），不是模型写的简写
        self.assertEqual(parse_shortlist(raw, labels), ["8241 杆塔", "8230-10 设计变更文件"])

    def test_parse_drops_unknown_and_garbage(self):
        labels = ["8230-10 设计变更文件"]
        self.assertEqual(parse_shortlist('{"cases":["不存在的一卷"]}', labels), [])
        self.assertEqual(parse_shortlist("我不知道", labels), [])
        self.assertEqual(parse_shortlist("", labels), [])

    def test_parse_respects_limit(self):
        labels = ["A卷", "B卷", "C卷", "D卷"]
        raw = '{"cases":["A卷","B卷","C卷","D卷"]}'
        self.assertEqual(len(parse_shortlist(raw, labels, limit=2)), 2)

    def test_parse_accepts_fenced_json(self):
        labels = ["8231 基础工程"]
        self.assertEqual(parse_shortlist('```json\n{"cases":["8231 基础工程"]}\n```', labels),
                         ["8231 基础工程"])


class CaseIndexTest(unittest.TestCase):
    def test_groups_names_by_case(self):
        class Rec:
            def __init__(self, rel):
                self.rel_path = rel

        files = [Rec("施工/8231 基础工程/1.验收记录.pdf"),
                 Rec("施工/8231 基础工程/2.竣工图.pdf"),
                 Rec("施工/8232 杆塔/3.组立记录.pdf")]
        index, truncated = case_index(files)
        self.assertFalse(truncated)
        self.assertEqual(index["8231 基础工程"], ["1.验收记录.pdf", "2.竣工图.pdf"])
        self.assertEqual(len(index), 2)

    def test_truncates(self):
        class Rec:
            def __init__(self, rel):
                self.rel_path = rel

        files = [Rec(f"施工/卷{i}/a.pdf") for i in range(5)]
        index, truncated = case_index(files, limit=2)
        self.assertTrue(truncated)
        self.assertEqual(len(index), 2)


class AskTest(unittest.TestCase):
    def test_ask_returns_raw_and_hits_cache(self):
        session = FakeSession(FakeResponse(openai_body("随便一段话")))
        judge = judge_with(session)
        first = judge.ask("prompt")
        self.assertEqual(first.raw, "随便一段话")
        self.assertFalse(first.cached)
        second = judge.ask("prompt")
        self.assertTrue(second.cached)
        self.assertEqual(len(session.calls), 1)

    def test_same_prompt_different_system_is_a_miss(self):
        session = FakeSession(FakeResponse(openai_body("{}")))
        judge = judge_with(session)
        judge.ask("prompt")
        judge.ask("prompt", "另一个系统提示")
        self.assertEqual(len(session.calls), 2)


class JudgeRequestTest(unittest.TestCase):
    """两段式：先指认案卷，再核对那一卷的文件名。"""

    CASES = [("8230-10 设计变更文件", 12), ("8231 基础工程", 8)]
    INDEX = {
        "8230-10 设计变更文件": ["图纸A.pdf", "零件加工图.pdf"],
        "8231 基础工程": ["1.验收记录.pdf"],
    }

    def request(self, **kwargs):
        return JudgeRequest(
            result=make_result(evidence=()),
            excerpt=EXCERPT,
            cases=tuple(self.CASES),
            case_map=dict(self.INDEX),
            **kwargs,
        )

    def test_second_call_carries_picked_case_files(self):
        session = FakeSession(
            FakeResponse(openai_body('{"cases":["8230-10"],"reason":"图纸可能在变更卷"}')),
            FakeResponse(openai_body(
                '{"verdict":"match","confidence":0.8,"reason":"并卷在变更卷",'
                '"quote":"零件加工图.pdf"}')),
        )
        judge = judge_with(session)
        verdict = judge_request(judge, self.request())
        self.assertEqual(verdict.verdict, "match")
        self.assertEqual(len(session.calls), 2)
        second = session.calls[1]["body"]["messages"][1]["content"]
        self.assertIn("案卷：8230-10 设计变更文件", second)
        self.assertIn("零件加工图.pdf", second)
        self.assertNotIn("1.验收记录.pdf", second)
        self.assertIn("先指认案卷", verdict.reason)

    def test_first_stage_sees_case_list(self):
        session = FakeSession(
            FakeResponse(openai_body('{"cases":[],"reason":"不该由本单位归档"}')),
            FakeResponse(openai_body(
                '{"verdict":"gap","confidence":0.7,"reason":"确实没有","quote":""}')),
        )
        judge = judge_with(session)
        verdict = judge_request(judge, self.request())
        first = session.calls[0]["body"]["messages"][1]["content"]
        self.assertIn("8230-10 设计变更文件（12）", first)
        self.assertNotIn("案卷：8230-10", first)  # 第一段不给卷内文件
        self.assertEqual(verdict.verdict, "gap")
        self.assertEqual(verdict.reason, "确实没有")

    def test_empty_shortlist_falls_back_to_lexical_folders(self):
        session = FakeSession(
            FakeResponse(openai_body('{"cases":[],"reason":"不确定"}')),
            FakeResponse(openai_body(
                '{"verdict":"unclear","confidence":0.2,"reason":"看不懂","quote":""}')),
        )
        judge = judge_with(session)
        request = self.request(fallback_folders=(("8231 基础工程", ["1.验收记录.pdf"]),))
        judge_request(judge, request)
        second = session.calls[1]["body"]["messages"][1]["content"]
        self.assertIn("案卷：8231 基础工程", second)

    def test_first_stage_failure_does_not_kill_the_item(self):
        session = FakeSession(
            FakeResponse({"error": "boom"}, status_code=500),
            FakeResponse({"error": "boom"}, status_code=500),
            FakeResponse({"error": "boom"}, status_code=500),
            FakeResponse(openai_body(
                '{"verdict":"gap","confidence":0.6,"reason":"缺","quote":""}')),
        )
        judge = judge_with(session)
        verdict = judge_request(judge, self.request())
        self.assertEqual(verdict.verdict, "gap")

    def test_truncated_case_list_skips_first_stage(self):
        session = FakeSession(FakeResponse(openai_body(
            '{"verdict":"gap","confidence":0.6,"reason":"缺","quote":""}')))
        judge = judge_with(session)
        request = self.request(cases_truncated=True)
        judge_request(judge, request)
        self.assertEqual(len(session.calls), 1)

    def test_quote_from_unselected_case_is_rejected(self):
        # 模型引用了这次没给它的卷里的文件名 → 说明是脑补，降级
        session = FakeSession(
            FakeResponse(openai_body('{"cases":["8231"],"reason":"基础"}')),
            FakeResponse(openai_body(
                '{"verdict":"match","confidence":0.8,"reason":"并卷","quote":"零件加工图.pdf"}')),
        )
        judge = judge_with(session)
        verdict = judge_request(judge, self.request())
        self.assertEqual(verdict.verdict, "unclear")
        self.assertIn("quote 校验未通过", verdict.error)


if __name__ == "__main__":
    unittest.main()
