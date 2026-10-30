"""正文证据：用 PDF/扫描件里读到的文字，补强或纠正文件名匹配。

只有在文件名证据不足（疑似）时才值得付出 OCR 成本，所以这里只处理
“已经有候选文件”的条目——文件名都没找到候选的，读正文也多半是白读。

判定链：
1. 每个候选文件读到足够多页后，按检查点关键词覆盖率算正文分；
2. 正文分够高、且文书类型也在正文里出现，才把“疑似”提升为“已匹配”；
3. 正文里完全找不到清单字样时，只写提示，不降级（文件名仍是有效证据）；
4. 需要盖章的文书类型，附带红色印章检测结论。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .matcher import DOC_FAMILIES, Matcher
from .models import (
    STATE_MATCHED,
    STATE_MISSING,
    STATE_SUSPECT,
    Checkpoint,
    Evidence,
    FileRecord,
    MatchResult,
)
from .pdf_reader import SUPPORTED_EXTENSIONS, ContentReader, DocumentContent
from .text import DOC_TERMS, clean_for_match, extract_terms, strip_serial

# 正文分达到该值且文书类型命中，才把“疑似”升级为“已匹配”
CONTENT_MATCH = 0.55
# 读到该分数即可停止翻页（已足够确认）
CONTENT_STOP = 0.80
# 正文分低于该值视为“正文与清单对不上”，只提示不改状态
CONTENT_WEAK = 0.12
# 正文最多累计到这么多字就停，避免长报告白读
CONTENT_MAX_CHARS = 6000
# 深度模式：未归类文件正文分达到该值、且领先第二名这么多，才救成“疑似”
DEEP_MATCH = 0.70
DEEP_MARGIN = 0.10
DEEP_MAX_PAGES = 2
# 文件名得分低于该值的文件视为“未归类”，才进深度模式
SUSPECT_FLOOR = 0.40

# 这类文书按业务习惯需要盖章
SEAL_DOC_TERMS = (
    "报审表", "报验表", "申请表", "签证", "验收记录", "检查记录", "验收表",
    "评定表", "合格证", "质量证明", "试验报告", "检测报告", "通知书", "承诺书",
    "委托书", "任命书", "协议", "合同", "交底", "竣工图", "开工报告", "意见",
    "审批", "批复", "备案", "移交",
)

ProgressFn = Callable[[int, int, str], None]


@dataclass
class ContentStats:
    enabled: bool = False
    files_read: int = 0
    files_requested: int = 0
    ocr_pages: int = 0
    text_pages: int = 0
    early_stops: int = 0
    cache_hits: int = 0
    promoted: int = 0
    weak: int = 0
    gated: int = 0
    relaxed: int = 0
    seals_found: int = 0
    seals_missing: int = 0
    seals_unknown: int = 0
    deep_files: int = 0
    rescued: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "files_read": self.files_read,
            "files_requested": self.files_requested,
            "ocr_pages": self.ocr_pages,
            "text_pages": self.text_pages,
            "early_stops": self.early_stops,
            "cache_hits": self.cache_hits,
            "promoted": self.promoted,
            "weak": self.weak,
            "gated": self.gated,
            "relaxed": self.relaxed,
            "seals_found": self.seals_found,
            "seals_missing": self.seals_missing,
            "seals_unknown": self.seals_unknown,
            "deep_files": self.deep_files,
            "rescued": self.rescued,
            "seconds": round(self.seconds, 1),
            "errors": self.errors[:20],
        }


def requires_seal(cp: Checkpoint) -> bool:
    return any(term in cp.text for term in SEAL_DOC_TERMS)


def _cp_terms(cp: Checkpoint) -> list[str]:
    terms: list[str] = []
    for term in list(extract_terms(cp.text)) + list(cp.keywords):
        cleaned = clean_for_match(term)
        if len(cleaned) >= 2 and cleaned not in terms:
            terms.append(cleaned)
    return terms[:60]


def _cp_doc_terms(cp: Checkpoint) -> list[str]:
    return [clean_for_match(term) for term in DOC_TERMS if term in cp.text]


def _bigram_coverage(term: str, text: str) -> float:
    if len(term) < 4:
        return 0.0
    grams = {term[i:i + 2] for i in range(len(term) - 1)}
    if not grams:
        return 0.0
    hit = sum(1 for gram in grams if gram in text)
    return hit / len(grams)


# 文书类型词：判断“主语是否对得上”时要先摘掉，否则“记录/报审表”会把结论带偏
_DOC_WORDS = tuple(
    sorted(
        set(DOC_TERMS) | {word for family in DOC_FAMILIES for word in family},
        key=len,
        reverse=True,
    )
)


def _relax(text: str) -> str:
    """按匹配器的同义词表归一化：铁塔/杆塔、架设/架线、消缺/缺陷……"""
    for old, new in Matcher.SYNONYMS:
        if old in text:
            text = text.replace(old, new)
    return text


_FILLERS = "及等的与和附加"


def _subject_chunks(cp: Checkpoint) -> list[str]:
    """把清单条目拆成若干“主语块”，文书类型词和并列连词都当分隔符。

    “工程竣工验收消缺清单及闭环”拆成“工程竣工缺陷”“闭环”两块：
    现场写“验收发现缺陷及处理情况汇总表”命中前一块，就不该再提示复核。
    """
    text = _relax(clean_for_match(cp.text))
    for word in _DOC_WORDS:
        text = text.replace(word, "|")
    text = text.replace("及", "|")
    chunks = []
    for chunk in text.split("|"):
        chunk = chunk.strip(_FILLERS)
        if len(chunk) >= 2:
            chunks.append(chunk)
    return chunks


def subject_coverage(cp: Checkpoint, text_norm: str) -> float:
    """清单“主语”在正文里的覆盖度（已摘掉文书类型词）。

    “杆塔分部工程开工”对正文“铁塔组立分部开工报告”这种同义/换序写法，
    整句短语匹配不上，但主语明显对得上——这种情况不该提示“正文未见清单字样”。
    """
    chunks = _subject_chunks(cp)
    if not chunks:
        return 1.0
    text = _relax(text_norm)
    hit = 0
    for chunk in chunks:
        if (
            chunk in text
            or Matcher._bigram_coverage(chunk, text) >= 0.6
            or Matcher._anchor_hit(chunk, text)
        ):
            hit += 1
    return hit / len(chunks)


def _term_present(term: str, text: str) -> bool:
    """区分词是否真的在文本里。

    这里刻意不用首尾锚点：`施工用混凝土` 靠“施工/混凝土”两个锚点也能在
    `…施工部位商品混凝土检测报告` 里蒙过去，区分词就失去意义了。
    """
    if not term:
        return True
    return term in text or Matcher._bigram_coverage(term, text) >= 0.75


def distinguishing_terms(results: list[MatchResult]) -> dict[str, str]:
    """同一条“主要项目文件”写法、归档内容不同的检查点，各自的区分词。

    例：“见证取样记录”在 05施工用混凝土 / 06商品混凝土 / 07钢筋 /
    09钢筋焊接或机械连接报告 四条里写法完全一样，区分它们的是归档内容。
    正文升级必须命中自己的区分词——否则一份“商品混凝土取样记录”会把
    钢筋、钢筋焊接这几条一起升成已匹配。
    """
    groups: dict[str, list[Checkpoint]] = defaultdict(list)
    for result in results:
        cp = result.checkpoint
        if cp.archive_content.strip():
            groups[clean_for_match(cp.text)].append(cp)
    out: dict[str, str] = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        if len({clean_for_match(cp.archive_content) for cp in items}) < 2:
            continue
        for cp in items:
            own = strip_serial(clean_for_match(cp.archive_content))
            own = _relax(own) if own else ""
            if len(own) >= 2:
                out[cp.id] = own
    return out


def score_content(
    cp: Checkpoint, text_norm: str, terms: list[str] | None = None
) -> tuple[float, list[str]]:
    """正文与检查点的关键词覆盖率（0–1）及命中的词。"""
    if not text_norm:
        return 0.0, []
    terms = terms if terms is not None else _cp_terms(cp)
    if not terms:
        return 0.0, []

    total = 0.0
    matched = 0.0
    hits: list[str] = []
    for term in terms:
        weight = min(len(term), 8)
        total += weight
        if term in text_norm:
            matched += weight
            hits.append(term)
        elif _bigram_coverage(term, text_norm) >= 0.75:
            matched += weight * 0.5
            hits.append(term)
    score = matched / total if total else 0.0

    # 要的文书类型在正文里完全没出现，说明可能读到了同卷的别的文件
    doc_terms = _cp_doc_terms(cp)
    if doc_terms and not any(term in text_norm for term in doc_terms):
        score *= 0.6
    return round(score, 3), hits[:6]


def _snippet(text: str, hits: list[str], width: int = 60) -> str:
    """从正文里截一段能看到命中词的片段，供人工复核。"""
    flat = "".join(text.split())
    if not flat:
        return ""
    for term in hits:
        position = flat.find(term)
        if position >= 0:
            start = max(0, position - width // 2)
            piece = flat[start:start + width]
            return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")
    return flat[:width] + ("…" if len(flat) > width else "")


class _StopRule:
    """读到任意一个候选检查点已经足够确认时停止翻页。"""

    def __init__(self, candidates: list[tuple[MatchResult, list[str]]]) -> None:
        self.candidates = candidates

    def __call__(self, text: str, page: int) -> bool:
        if len(text.replace("\n", "")) >= CONTENT_MAX_CHARS:
            return True
        if page < 1:
            return False
        norm = clean_for_match(text)
        for result, terms in self.candidates:
            score, _ = score_content(result.checkpoint, norm, terms)
            if score >= CONTENT_STOP:
                return True
        return False


def enrich_with_content(
    results: list[MatchResult],
    files: list[FileRecord],
    root: str | Path,
    reader: ContentReader,
    *,
    max_files: int = 80,
    check_seal: bool = True,
    deep: bool = False,
    deep_max_files: int = 40,
    respect_ids: set[str] | None = None,
    progress: ProgressFn | None = None,
) -> ContentStats:
    """对疑似项的候选文件读正文；命中则升级，并附带印章检测结论。"""
    stats = ContentStats(enabled=True)
    respect_ids = respect_ids or set()
    by_path = {file.rel_path: file for file in files}
    root_path = Path(root)

    # 需要读正文的条目：疑似（要确认）；需要盖章的条目（要查章）
    targets: list[MatchResult] = []
    for result in results:
        if result.state == STATE_SUSPECT and result.evidence:
            targets.append(result)
        elif check_seal and requires_seal(result.checkpoint) and result.evidence:
            targets.append(result)

    # 先把疑似项的证据文件排前面，名额不够时优先保证它们
    priority = {id(result): 0 if result.state == STATE_SUSPECT else 1 for result in targets}
    targets.sort(key=lambda item: priority[id(item)])

    wanted: dict[str, list[MatchResult]] = {}
    for result in targets:
        for evidence in result.evidence[:2]:
            wanted.setdefault(evidence.rel_path, []).append(result)

    stats.files_requested = len(wanted)
    order = sorted(wanted, key=lambda path: min(priority[id(r)] for r in wanted[path]))
    order = order[:max_files]

    terms_cache: dict[int, list[str]] = {}
    docs: dict[str, DocumentContent] = {}

    for index, rel_path in enumerate(order, start=1):
        file = by_path.get(rel_path)
        if file is None:
            continue
        candidates = wanted[rel_path]
        for result in candidates:
            terms_cache.setdefault(id(result), _cp_terms(result.checkpoint))
        stop_rule = _StopRule(
            [(result, terms_cache[id(result)]) for result in candidates]
        )
        document = reader.read(
            root_path / rel_path, rel_path=rel_path, stop_when=stop_rule
        )
        docs[rel_path] = document
        if document.error:
            stats.errors.append(f"{rel_path}: {document.error}")
        else:
            stats.early_stops += int(document.stopped_early)
            stats.text_pages += document.read_pages - document.ocr_pages
        if progress:
            progress(index, len(order), rel_path)

    stats.files_read = len(docs)
    stats.ocr_pages = reader.ocr_pages
    stats.seconds = reader.seconds
    stats.cache_hits = reader.hits

    # ---- 正文判定 -------------------------------------------------------
    distinct = distinguishing_terms(results)
    for result in results:
        if not result.evidence:
            continue
        primary = result.evidence[0].rel_path
        best: tuple[float, str, DocumentContent, list[str]] | None = None
        for evidence in result.evidence[:2]:
            document = docs.get(evidence.rel_path)
            if document is None or document.error or not document.chars:
                continue
            terms = terms_cache.setdefault(id(result), _cp_terms(result.checkpoint))
            score, hits = score_content(result.checkpoint, clean_for_match(document.text), terms)
            if best is None or score > best[0]:
                best = (score, evidence.rel_path, document, hits)
        if best is None:
            continue
        score, best_rel, document, hits = best
        text_norm = clean_for_match(document.text)
        result.content_score = score
        result.content_pages = document.read_pages
        result.content_ocr = document.used_ocr
        result.content_snippet = _snippet(document.text, hits)

        if result.checkpoint.id in respect_ids:
            continue  # 人工已纠正过的条目，正文不覆盖人工判定

        gate = distinct.get(result.checkpoint.id)
        if gate:
            evidence_name = _relax(clean_for_match(Path(result.evidence[0].rel_path).name))
            if not (_term_present(gate, text_norm) or _term_present(gate, evidence_name)):
                # 同组检查点靠归档内容区分（05施工用混凝土 / 07钢筋…），
                # 正文和文件名都没有自己的区分词，不能升。
                stats.gated += 1
                continue

        if result.state == STATE_SUSPECT and score >= CONTENT_MATCH and hits:
            result.state = STATE_MATCHED
            result.score = max(result.score, 0.70)
            result.content_confirmed = True
            result.reason = (
                f"正文命中：{'、'.join(hits[:4])}（正文分 {score:.2f}，"
                f"读 {document.read_pages} 页）"
            )
            if result.manual:
                result.reason = "人工纠正：" + result.reason
            stats.promoted += 1
        elif result.state == STATE_MATCHED and score < CONTENT_WEAK:
            # 只在读的确实是第 1 候选文件时，才敢说“正文对不上”；
            # 退而读第 2 候选算出来的低分，说明不了主证据的情况。
            if best_rel != primary:
                continue
            if subject_coverage(result.checkpoint, text_norm) >= 0.5:
                stats.relaxed += 1  # 主语在正文里，只是写法不同，不提示复核
                continue
            result.reason += "；正文未见清单字样，建议复核"
            stats.weak += 1
        if not result.auto_state:
            result.auto_state = result.state
            result.auto_reason = result.reason

    # ---- 印章判定 -------------------------------------------------------
    if check_seal:
        for result in results:
            if not result.evidence:
                continue
            if not requires_seal(result.checkpoint):
                continue
            document = docs.get(result.evidence[0].rel_path)
            if document is None or document.error:
                continue
            result.needs_seal = True
            result.seal_state = document.seal_state
            result.seal_pages = list(document.seal_pages)
            result.seal_count = document.seal_count
            if document.seal_state == "有":
                stats.seals_found += 1
            elif document.seal_state == "无":
                stats.seals_missing += 1
                result.reason += "；未见红色印章（该文种通常需盖章）"
            else:
                stats.seals_unknown += 1

    if deep:
        _deep_scan(results, files, root_path, reader, stats,
                   max_files=deep_max_files, respect_ids=respect_ids,
                   distinct=distinct, progress=progress)
    return stats


def _deep_scan(
    results: list[MatchResult],
    files: list[FileRecord],
    root_path: Path,
    reader: ContentReader,
    stats: ContentStats,
    *,
    max_files: int = 40,
    respect_ids: set[str] | None = None,
    distinct: dict[str, str] | None = None,
    progress: ProgressFn | None = None,
) -> None:
    """深度模式：把未归类文件也读一遍，用正文去救“缺失”项。

    文件名完全对不上、但正文标题能对上的情况（扫描件命名成“1.pdf”）只有这条路能救。
    救回来的只判“疑似”，不直接算已匹配——正文里常会顺带提到别的文种。
    """
    missing = [result for result in results if result.state == STATE_MISSING]
    if not missing:
        return
    terms_map = {id(result): _cp_terms(result.checkpoint) for result in missing}
    orphans = [
        file for file in files
        if file.best_score < SUSPECT_FLOOR and file.ext.lower() in SUPPORTED_EXTENSIONS
    ]
    orphans.sort(key=lambda file: file.best_score, reverse=True)
    orphans = orphans[:max_files]
    if not orphans:
        return

    previous_pages = reader.max_pages
    reader.max_pages = min(previous_pages, DEEP_MAX_PAGES)
    try:
        for index, file in enumerate(orphans, start=1):
            document = reader.read(root_path / file.rel_path, rel_path=file.rel_path)
            stats.deep_files += 1
            if document.error or not document.chars:
                continue
            norm = clean_for_match(document.text)
            scored: list[tuple[float, MatchResult, list[str]]] = []
            for result in missing:
                score, hits = score_content(
                    result.checkpoint, norm, terms_map[id(result)]
                )
                if score > 0:
                    scored.append((score, result, hits))
            if not scored:
                continue
            scored.sort(key=lambda item: item[0], reverse=True)
            best_score, best_result, hits = scored[0]
            second = scored[1][0] if len(scored) > 1 else 0.0
            if best_score < DEEP_MATCH or best_score - second < DEEP_MARGIN:
                continue
            if best_result.manual or best_result.checkpoint.id in (respect_ids or set()):
                continue
            gate = (distinct or {}).get(best_result.checkpoint.id)
            if gate:
                name_norm = _relax(clean_for_match(Path(file.rel_path).name))
                if not (_term_present(gate, norm) or _term_present(gate, name_norm)):
                    continue
            best_result.state = STATE_SUSPECT
            best_result.score = max(best_result.score, round(best_score, 3))
            best_result.content_score = round(best_score, 3)
            best_result.content_pages = document.read_pages
            best_result.content_ocr = document.used_ocr
            best_result.content_snippet = _snippet(document.text, hits)
            best_result.evidence = [Evidence(rel_path=file.rel_path, score=best_score)]
            best_result.reason = (
                f"正文疑似：{'、'.join(hits[:4])}（正文分 {best_score:.2f}，"
                f"文件名未命中，建议开卷确认）"
            )
            if not best_result.auto_state:
                best_result.auto_state = best_result.state
                best_result.auto_reason = best_result.reason
            stats.rescued += 1
            if progress:
                progress(index, len(orphans), file.rel_path)
    finally:
        reader.max_pages = previous_pages
