"""文件名/路径匹配器。

打分 = 分类编码 20% + 关键词覆盖率 45% + 字符 3-gram 覆盖率 35%
编码一致是强信号，但不会单独把一条判成“已匹配”：
只有文本证据也达到最低要求，才进入“已匹配”，否则降为“疑似”。
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import (
    STATE_MATCHED,
    STATE_MISSING,
    STATE_NA,
    STATE_SUSPECT,
    Checkpoint,
    Evidence,
    FileRecord,
    MatchResult,
)
from .text import (
    DOC_TERMS,
    STOPWORDS,
    char_ngrams,
    clean_for_match,
    detect_common_prefix,
    extract_terms,
    strip_serial,
)

ProgressFn = Callable[[int, int], None]

# 类目名里的“综合/项目施工”等只表示章节，不能用来区分具体文件
CONTEXT_IGNORE = {
    "综合", "项目施工", "项目准备", "施工监理", "启动验收", "竣工验收",
    "基础工程", "工程开工及报审", "其他",
}

# 同一类文书的常见叫法：清单写“试验报告”，现场可能写“检测报告”
DOC_FAMILIES = (
    {"试验报告", "检测报告", "检验报告", "试验", "检测"},
    {"验收记录", "检查记录", "验收表", "评定表", "验收", "评级记录", "质量验收记录"},
    {"报审表", "报审", "报批", "审批", "报验表"},
    {"开工报告", "开工报审", "开工"},
    {"合格证", "质量证明", "证明文件", "出厂证明"},
    {"竣工图", "图纸", "施工图"},
    {"记录", "记录表", "台账"},
    {"清单", "汇总表", "明细表", "统计表"},
)


@dataclass
class Scored:
    index: int
    score: float
    code_score: float
    token_score: float
    char_score: float
    boost: str = ""


@dataclass(frozen=True)
class DocGroup:
    """检查点里的一份/一类文件，如“项目部成立”“公章启用文件”。"""

    text: str
    subject: str
    docs: tuple[str, ...]


class Matcher:
    MATCH_THRESHOLD = 0.62
    SUSPECT_THRESHOLD = 0.40
    MAX_CANDIDATES = 400
    MAX_PER_FILE = 30
    # 同分类号文件夹内，单个分句的语义覆盖达到该值才算“找到对应文件”
    FOLDER_GROUP_THRESHOLD = 0.60

    # 现场写法与清单用词的常见差异，仅在字符覆盖兜底时归一化
    SYNONYMS = (
        ("项目经理部", "项目部"),
        ("印章", "公章"),
        ("组建", "成立"),
        ("报批", "报审"),
        ("起用", "启用"),
        ("函件", "文件"),
        ("函", "文件"),
        ("铁塔", "杆塔"),
        ("桩基础", "桩基"),
        ("盘测", "单盘测试"),
        ("立塔", "组立"),
        ("架设", "架线"),
        ("消缺", "缺陷"),
        # 现场“复合光缆OPGW展放施工”与清单“OPGW光缆展放施工”语序相反；
        # 不归一化时词首前缀只剩“OPGW”，主语长度 12 个字只命中 4 个。
        # 表里的键必须小写：clean_for_match 已把文本转小写。
        ("光缆opgw", "opgw光缆"),
    )

    # 清单主语里的泛化前缀，比对文件夹卷内目录时可以先去掉
    GENERIC_SUBJECT_PREFIXES = (
        "各专项", "各项", "有关", "相关", "项目", "工程", "施工", "建设", "含", "各",
    )
    # 泛化后缀：“立塔施工”与文件夹里的“塔组立方案”只差一个“施工”；
    # “项目竣工验收”在现场常写成“竣工报告”
    GENERIC_SUBJECT_SUFFIXES = ("竣工验收", "验收", "施工")

    def __init__(
        self,
        checkpoints: list[Checkpoint],
        project_name: str = "",
        known_codes: list[str] | None = None,
    ) -> None:
        self.checkpoints = checkpoints
        self.project_name = clean_for_match(project_name)
        self.common_prefix = ""

        # 全部模版编码（含被项目类型过滤掉的），用于识别“文件带 8230、
        # 当前却在查 8330”这类跨项目类型误配，并压回疑似。
        self.known_prefixes: set[str] = set()
        for code in {cp.code for cp in checkpoints} | set(known_codes or []):
            for size in range(2, len(code) + 1):
                self.known_prefixes.add(code[:size])

        self.cp_terms: list[list[str]] = []
        self.cp_norm_terms: list[list[str]] = []
        self.cp_grams: list[set[str]] = []
        self.cp_context: list[list[str]] = []
        self.cp_docs: list[list[str]] = []
        self.cp_subjects: list[list[str]] = []
        self.cp_groups: list[list[str]] = []
        self.cp_doc_groups: list[list[DocGroup]] = []
        self.cp_needs_context: list[bool] = []
        self.cp_index: dict[str, set[int]] = defaultdict(set)
        self.by_code: dict[str, set[int]] = defaultdict(set)
        self.by_prefix: dict[str, set[int]] = defaultdict(set)
        df: Counter[str] = Counter()
        text_list = [clean_for_match(cp.text) for cp in checkpoints]
        text_counts = Counter(text_list)

        for index, cp in enumerate(checkpoints):
            # 只用“主要项目文件”本身做匹配依据。归档内容/类目名（如“综合”）
            # 往往不体现具体文件，混进来会稀释关键词覆盖率。
            terms = extract_terms(cp.text)
            terms.extend(cp.keywords)
            cleaned_terms: list[str] = []
            for term in terms:
                term = clean_for_match(term)
                if len(term) >= 2 and term not in cleaned_terms:
                    cleaned_terms.append(term)
            cleaned_terms = cleaned_terms[:60]
            self.cp_terms.append(cleaned_terms)
            self.cp_norm_terms.append(
                [self._normalize_text(term) for term in cleaned_terms]
            )
            for term in cleaned_terms:
                df[term] += 1
                self.cp_index[term].add(index)

            grams = char_ngrams(cp.text, 3)
            self.cp_grams.append(grams)
            for gram in grams:
                self.cp_index[gram].add(index)

            # “见证取样记录”这类文字在多个类目下重复出现，必须靠
            # 归档内容（水泥/砂/碎石/钢筋…）区分，否则会全部命中同一份文件。
            clean_text = clean_for_match(cp.text)
            self.cp_context.append(self._context_terms(cp.archive_content))
            self.cp_needs_context.append(
                text_counts[clean_text] > 1
                or any(
                    clean_text != other and clean_text in other
                    for other in text_list
                )
            )
            self.cp_docs.append([term for term in DOC_TERMS if term in cp.text])
            groups = [
                part.strip()
                for part in re.split(r"(?<=.)[及和与或](?=.)", cp.text)
                if part.strip()
            ]
            self.cp_groups.append(groups if len(groups) > 1 else [])
            self.cp_doc_groups.append(self._doc_groups(cp.text))
            self.cp_subjects.append(
                [
                    term
                    for term in cleaned_terms
                    if term not in DOC_TERMS
                    and term not in CONTEXT_IGNORE
                    and len(term) >= 2
                ]
            )

            self.by_code[cp.code].add(index)
            for size in range(2, len(cp.code) + 1):
                self.by_prefix[cp.code[:size]].add(index)

        total = max(1, len(checkpoints))
        self.idf = {term: math.log((total + 1) / (count + 1)) + 1 for term, count in df.items()}

    def set_file_context(self, files: Iterable[FileRecord]) -> None:
        stems = [strip_serial(item.stem) for item in files]
        self.common_prefix = clean_for_match(detect_common_prefix(stems))

    def _path_text(self, file: FileRecord) -> str:
        text = clean_for_match(file.rel_path)
        text = text.replace("扫描件", "")
        if self.project_name:
            text = text.replace(self.project_name, "")
        if self.common_prefix:
            text = text.replace(self.common_prefix, "")
        return text

    @staticmethod
    def _context_terms(value: str) -> list[str]:
        """从归档内容提取区分词，如“03施工用砂” → 施工用砂 / 砂。"""
        text = strip_serial(value)
        if not text:
            return []
        parts = re.split(r"[、，,；;：:（）()《》【】\[\]/\s或]+", text)
        out: list[str] = []
        for part in parts:
            part = part.strip()
            if len(part) < 2:
                continue
            variants = [(part, 2)]
            stripped = re.sub(r"^(施工用|施工|项目)", "", part)
            if stripped != part:
                # “施工用砂”去掉前缀后只剩单字“砂”，但它正是区分
                # 水泥/砂/碎石资料的关键词，放宽到 1 个字。
                variants.append((stripped, 1))
            for variant, min_len in variants:
                if len(variant) >= min_len and variant not in CONTEXT_IGNORE and variant not in out:
                    out.append(variant)
        return out

    @classmethod
    def _normalize_text(cls, value: str) -> str:
        text = clean_for_match(value)
        for old, new in cls.SYNONYMS:
            text = text.replace(old, new)
        return text

    @staticmethod
    def _anchor_hit(norm_term: str, norm_text: str) -> bool:
        """首尾锚点 + 中段锚点，处理换序但要素齐全的写法。

        例：“项目部成立”对“关于成立……项目部”，首“项目”尾“成立”都在；
        “含土地复垦方案报审”对“土地复垦方案及报审表”，首尾和中段都能对上。

        首尾必须同时出现，且 6 字以上还要命中一个中段二字词，
        避免“水泥进场报审表”靠“用水报告”里的“水/报审”蒙混过关。
        匹配长词时只传入**文件名**，避免拿文件夹名补字。
        """
        if len(norm_term) < 4:
            return False
        if norm_term[:2] not in norm_text or norm_term[-2:] not in norm_text:
            return False
        if len(norm_term) >= 6:
            middle = [
                norm_term[index:index + 2]
                for index in range(2, len(norm_term) - 2)
            ]
            if not any(part in norm_text for part in middle):
                return False
        return True

    @staticmethod
    def _bigram_coverage(norm_term: str, norm_text: str) -> float:
        grams = {norm_term[index:index + 2] for index in range(len(norm_term) - 1)}
        if not grams:
            return 0.0
        return sum(1 for gram in grams if gram in norm_text) / len(grams)

    def _filename_support(
        self, term: str, norm_name: str, norm_path: str
    ) -> bool:
        """长词是否由**文件名**支撑（换序/换词兜底专用）。

        只相信文件名，不相信文件夹：资料文件夹名往往把整类内容罗列一遍，
        拿文件夹补字会把“钢筋原材料检测试验报告”判成“地脚螺栓第三方
        试验报告”。唯一例外是“基础/土石方/接地”这类分部前缀可以来自
        文件夹，但文件名必须完整含住其余至少 5 个字——所以
        “基础分部工程开工”对文件夹“基础开工报告”+文件“分部工程开工
        报审表”成立，而“桩基检测报告”不会靠文件夹补成“商品混凝土检测”。
        """
        if (
            self._anchor_hit(term, norm_name)
            and self._bigram_coverage(term, norm_name) >= 0.4
        ):
            return True
        return len(term) >= 7 and term[:2] in norm_path and term[2:] in norm_name

    @classmethod
    def _doc_groups(cls, text: str) -> list[DocGroup]:
        """把检查点拆成若干“一份文件”的分句。

        “项目部成立、公章启用文件”是两份文件，可以分别命中；
        “杆塔分部工程开工及报审表”里“报审表”只是文书词，不单独成组。
        括号里的内容是限定/补充说明（“隐蔽工程(基础)签证”、“验收意见
        （环保、水保……）”），不能单独成组，否则“基础”这种两字限定词
        会把同卷任何基础资料都算成一份文件。
        """
        text = re.sub(r"[（(][^）)]*[）)]", "", text)

        def split_subject(piece: str) -> tuple[str, tuple[str, ...]]:
            norm = cls._normalize_text(piece)
            docs = tuple(term for term in DOC_TERMS if term in piece)
            subject = norm
            for doc in sorted(
                (cls._normalize_text(term) for term in docs),
                key=len,
                reverse=True,
            ):
                subject = subject.replace(doc, "")
            return subject.strip(), docs

        pieces: list[str] = []
        for part in re.split(r"[、，,；;：:（）()《》【】\[\]/\s]+", text):
            part = part.strip()
            if not part:
                continue
            subs = [
                item.strip()
                for item in re.split(r"(?<=.)[及和与](?=.)", part)
            ]
            if len(subs) > 1 and all(len(item) >= 2 for item in subs):
                merged: list[str] = []
                for item in subs:
                    subject, docs = split_subject(item)
                    if len(subject) < 2 or subject in STOPWORDS:
                        # “报审表/附件”这类文书词并回前一分句，避免把
                        # “安健环策划及报审”拆成两个检查点，丢掉“报审”要求。
                        if docs and merged:
                            merged[-1] = f"{merged[-1]}及{item}"
                        continue
                    merged.append(item)
                if merged:
                    pieces.extend(merged)
                else:
                    pieces.append(part)
            else:
                pieces.append(part)

        grouped: dict[str, DocGroup] = {}
        for piece in pieces:
            subject, docs = split_subject(piece)
            if len(subject) < 2 or subject in STOPWORDS:
                continue
            norm = cls._normalize_text(piece)
            previous = grouped.get(subject)
            if previous:
                grouped[subject] = DocGroup(
                    text=previous.text,
                    subject=subject,
                    docs=tuple(dict.fromkeys(previous.docs + docs)),
                )
            else:
                grouped[subject] = DocGroup(text=norm, subject=subject, docs=docs)
        return list(grouped.values())

    @staticmethod
    def _similarity(subject: str, norm_name: str) -> float:
        """分句主语与文件名的语义相近度（子序列覆盖 + 二字覆盖率）。

        比的是“主语有多少字按顺序出现在文件名里”，项目名前缀很长时
        不会把分数稀释掉；“铁塔组立分部开工报告”对“杆塔分部工程开工”
        能拿到 0.75，而“钢筋原材料检测试验报告”对“地脚螺栓第三方”为 0。
        """
        if not subject or not norm_name:
            return 0.0
        if subject in norm_name:
            return 1.0
        matcher = difflib.SequenceMatcher(None, subject, norm_name, autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        lcs_score = matched / len(subject)
        grams = {subject[index:index + 2] for index in range(len(subject) - 1)}
        bigram_score = (
            sum(1 for gram in grams if gram in norm_name) / len(grams)
            if grams
            else 0.0
        )
        return max(lcs_score, bigram_score)

    @classmethod
    def _doc_present(
        cls, docs: tuple[str, ...], norm_name: str
    ) -> bool:
        if not docs:
            return True
        family: set[str] = set(docs)
        for doc in docs:
            for group in DOC_FAMILIES:
                if doc in group:
                    family |= group
        return any(cls._normalize_text(term) in norm_name for term in family)

    @classmethod
    def _doc_strict(cls, docs: tuple[str, ...], norm_name: str) -> bool:
        """判分用的文书类型核对：原文词，或 4 字以上的同族词。

        `_doc_present` 认族里的上位词（检测／验收／记录），判“有没有这类东西”
        够用；但“检测方案”会靠“检测”冒充“检测报告”，判分要更严。
        """
        return any(
            cls._normalize_text(doc) in norm_name
            or cls._doc_family_hit(doc, norm_name) >= 1.0
            for doc in docs
        )

    @classmethod
    def _doc_family_hit(cls, term: str, norm_name: str) -> float:
        """文书类型词按“同一类文书”计分。

        清单要求的“检查及评级记录”，现场封皮常写“分项工程质量验收记录”，
        是同一张表；DOC_FAMILIES 里已归为一族，这里让关键词得分也认这个等价，
        否则单据类检查点永远差一档（评级记录 x 评级 两个词合计占一半权重）。
        只认文件名，不认文件夹名。只认 4 字以上的等价词：族里的“检测”
        “验收”“记录”这类上位词太宽，“检测方案”会被当成“检测报告”。
        """
        if len(term) < 4:
            return 0.0
        for group in DOC_FAMILIES:
            if term not in group:
                continue
            if any(
                other != term
                and len(other) >= 4
                and cls._normalize_text(other) in norm_name
                for other in group
            ):
                return 1.0
            return 0.0
        return 0.0

    @staticmethod
    def _head_present(variants: list[str], norm_name: str) -> float:
        """主语首词是否原样出现在文件名里（“架线分部”的“架线”）。

        同卷里几份“专项施工方案”主题相同、分类不同时，首词出现过的
        才是同一分部的那份（导线架设 ≠ 跨越施工），字面相近度不足以区分。
        """
        return 1.0 if any(
            len(variant) >= 4 and variant[:2] in norm_name
            for variant in variants
        ) else 0.0

    def _doc_bonus(
        self,
        raw: str,
        docs: tuple[str, ...],
        norm_name: str,
        raw_name: str = "",
    ) -> float:
        """证据排序用：清单原句/文书类型越原样出现在文件名里越优先。

        同一卷里既有“灌注桩基础施工方案及报审表”又有“基础检测方案”时，
        只有前者原样含住“基础施工方案”，应优先作为证据。同义词归一
        （桩基础→桩基）会把原句拆散，所以再拿未归一的文件名比一次。

        文书类型按**覆盖度**计分，而不是“沾到一种就算”：清单要“施工方案
        报审表”，同卷的“架线分部开工报告及报审表”只覆盖了报审表，不能
        压过真正带“施工方案”的文件。权重给得比主语相近度高，因为文书
        类型对不上就是另一份材料。
        """
        raw_norm = self._normalize_text(raw)
        if raw_norm and (raw_norm in norm_name or raw_norm in raw_name):
            return 0.60
        required: list[str] = []
        for doc in docs:
            term = self._normalize_text(doc)
            if not term:
                continue
            if any(term in other for other in required):
                continue
            required = [other for other in required if other not in term]
            required.append(term)
        if not required:
            return 0.0
        hit = sum(1 for term in required if term in norm_name)
        return 0.50 * hit / len(required)

    def _group_match(
        self, subject: str, norm_name: str, docs: tuple[str, ...]
    ) -> tuple[float, bool] | None:
        """文件夹内单个分句与文件名的匹配度，返回 (相近度, 文书类型是否命中)。

        主语首尾二字都必须在文件名里出现：否则“砂使用跟踪”会被
        “碎石使用跟踪记录”以 0.67 的相近度命中，“项目部成立”会被
        “项目部印章启用函”命中。首尾锚点正是区分“砂/碎石”“成立/启用”
        的关键。文书类型只影响是否算“完整覆盖”，不命中时仍可作为疑似候选。
        没有文书类型可核对的“分句”（如“工程联系单”“项目部成立”）必须
        完整出现在文件名里：项目名里的“工程”会把“工程联系单”凑成
        “设计变更联系单”。
        """
        if not docs:
            if len(subject) == 2:
                if subject not in norm_name:
                    return None
                return 1.0, True
            if subject[:2] not in norm_name or subject[-2:] not in norm_name:
                return None
            similarity = self._similarity(subject, norm_name)
            if similarity < self.FOLDER_GROUP_THRESHOLD:
                return None
            # 完整成句才算找到；只有首尾相近的只能当疑似候选
            return similarity, subject in norm_name
        if len(subject) == 2:
            if subject not in norm_name:
                return None
            similarity = 1.0
        else:
            if subject[:2] not in norm_name or subject[-2:] not in norm_name:
                return None
            similarity = self._similarity(subject, norm_name)
            if similarity < self.FOLDER_GROUP_THRESHOLD:
                return None
        return similarity, self._doc_present(docs, norm_name)

    @classmethod
    def _subject_variants(cls, subject: str) -> list[str]:
        """主语去掉“项目/工程/各专项”这类泛化前后缀后的写法。"""
        variants = [subject]
        current = subject
        changed = True
        while changed:
            changed = False
            for prefix in cls.GENERIC_SUBJECT_PREFIXES:
                if current.startswith(prefix) and len(current) - len(prefix) >= 2:
                    current = current[len(prefix):]
                    if current not in variants:
                        variants.append(current)
                    changed = True
                    break
        for variant in list(variants):
            for suffix in cls.GENERIC_SUBJECT_SUFFIXES:
                if variant.endswith(suffix) and len(variant) - len(suffix) >= 2:
                    trimmed = variant[: -len(suffix)]
                    if trimmed not in variants:
                        variants.append(trimmed)
        return variants

    @staticmethod
    def _anchor_in_text(subject: str, text: str) -> bool:
        """主语首尾二字是否都在文本里出现（用于文件夹卷内目录标签）。"""
        if len(subject) < 2 or not text:
            return False
        if len(subject) == 2:
            return subject in text
        return subject[:2] in text and subject[-2:] in text

    @staticmethod
    def _edge_in_text(subject: str, text: str) -> bool:
        """主语首字或尾字是否出现在文件名里（防止卷内目录把整卷文件都算上）。"""
        if len(subject) < 2 or not text:
            return False
        if len(subject) == 2:
            return subject in text
        return subject[:2] in text or subject[-2:] in text

    def _folder_fallback(
        self, results: list[MatchResult], files: list[FileRecord]
    ) -> None:
        """按分类号锁定文件夹，再在文件夹内做语义匹配。

        现场资料常把同一类文件放在 `AX-8233-1 …` 这样的文件夹里，
        文件名和清单不必逐字相同。判定分三种：

        - 主语与**文件名**相近、文书类型也在文件名里 → 这一分句算找到；
        - 文件夹名（卷内目录标签）同时含主语和文书类型、文件名也有该文书
          类型 → 也算找到。清单常把“施工方案、作业指导书、执行文件”写在
          一条里，而实际只归了一卷施工方案，不能因为附件没单独成卷就判缺失；
        - 文件名里只有相近主语、文书类型对不上 → 只能进疑似，附候选文件。

        只要有一个分句找到就判已匹配；清单里没单独找到的分句写进理由，
        由人工复核，不再要求每个分句都出现。
        """
        folder_map: dict[tuple[str, ...], list[FileRecord]] = defaultdict(list)
        for file in files:
            if file.dir_parts:
                folder_map[tuple(file.dir_parts)].append(file)
        raw_names = {id(file): clean_for_match(file.name) for file in files}
        norm_names = {
            id(file): self._normalize_text(raw_names[id(file)])
            for file in files
        }

        for index, result in enumerate(results):
            if result.state == STATE_MATCHED:
                continue
            groups = self.cp_doc_groups[index]
            if not groups:
                continue
            cp = result.checkpoint
            excluded = [clean_for_match(term) for term in cp.exclude_terms]
            context_terms = self.cp_context[index]
            needs_context = self.cp_needs_context[index] and bool(context_terms)

            best_folder: tuple[str, ...] = ()
            best_full: dict[int, tuple[FileRecord, float]] = {}
            best_any: dict[int, tuple[FileRecord, float]] = {}
            for folder, items in folder_map.items():
                # 只认目录名里带本检查点分类号的文件夹（如 AX-8233-1）
                if not any(cp.code in part for part in folder):
                    continue
                if needs_context and not (
                    any(term in part for term in context_terms for part in folder)
                    or any(
                        term in norm_names[id(file)]
                        for term in context_terms
                        for file in items
                    )
                ):
                    continue

                folder_norm = self._normalize_text(folder[-1]) if folder else ""
                full: dict[int, tuple[FileRecord, float]] = {}
                any_hit: dict[int, tuple[FileRecord, float]] = {}
                for group_index, group in enumerate(groups):
                    variants = self._subject_variants(
                        self._normalize_text(group.subject)
                    )
                    folder_subject_ok = bool(folder_norm) and any(
                        self._anchor_in_text(variant, folder_norm)
                        for variant in variants
                    )
                    folder_doc_ok = bool(group.docs) and self._doc_present(
                        group.docs, folder_norm
                    )
                    pick_full: tuple[FileRecord, float] | None = None
                    # 证据排序键 (文书类型覆盖度, 主语相近度, 文件名长度)：
                    # 文书类型是文件的**主题内容**（施工方案 ≠ 开工报告），
                    # 优先于主语；主语（架线分部）只是**分类**，同卷里几份
                    # 文件都能沾上，不能拿它压过主题。
                    pick_rank: tuple[float, float, float] | None = None
                    pick_any: tuple[FileRecord, float] | None = None
                    for file in items:
                        norm_name = norm_names[id(file)]
                        if any(term in norm_name for term in excluded):
                            continue
                        matched = self._group_match(
                            group.subject, norm_name, group.docs
                        )
                        if not matched:
                            continue
                        similarity, doc_ok = matched
                        if pick_any is None or similarity > pick_any[1]:
                            pick_any = (file, similarity)
                        if doc_ok:
                            # 文书类型先于主语：“架线分部工程施工方案报审表”
                            # 取带“施工方案”的文件，而不是同卷的开工报告；
                            # 主语只用于同主题下的排序。
                            rank = (
                                self._doc_bonus(
                                    group.text,
                                    group.docs,
                                    norm_name,
                                    raw_names[id(file)],
                                ),
                                self._head_present(variants, norm_name),
                                similarity,
                                -len(norm_name),
                            )
                            if pick_rank is None or rank > pick_rank:
                                pick_rank = rank
                                pick_full = (file, similarity)
                    # 卷内目录标签（文件夹名）已写明该分句的主语和文书类型，
                    # 文件夹里也确实有这种文书：按这一分句“找到”处理。
                    if pick_full is None and folder_subject_ok and folder_doc_ok:
                        for file in items:
                            norm_name = norm_names[id(file)]
                            if any(term in norm_name for term in excluded):
                                continue
                            if not self._doc_present(group.docs, norm_name):
                                continue
                            # 卷内目录写明了主语，但具体文件也得沾点边：
                            # “桩基检测报告”不能靠同卷的“商品混凝土检测报告”
                            # 凑数，否则整卷文件都会被算成同一份。
                            if not any(
                                self._edge_in_text(variant, norm_name)
                                for variant in variants
                            ):
                                continue
                            # 主语要和“卷内目录 + 本文件名”整体对得上，
                            # 不能只因为同卷就归进来（光缆架设开工报告 ≠ 光缆衰减测试报告）。
                            combined = folder_norm + norm_name
                            if max(
                                self._similarity(variant, combined)
                                for variant in variants
                            ) < self.FOLDER_GROUP_THRESHOLD:
                                continue
                            # 证据文件排序：文件名自己越像主语越优先，
                            # 其次看主语首词（竣工报告 > 三级验收报告），
                            # 同分时短文件名优先（少一串“专家论证审查表”）。
                            # 主语变体按长度开方加权：去掉修饰词后的“基础”
                            # 这类两字变体会命中整卷文件，不能和“基础施工”
                            # 等权（否则“检测方案”会挤掉“灌注桩基础施工方案”）；
                            # 但“竣工”这种关键短词仍要保留分量（竣工报告 >
                            # 三级验收报告）。
                            full_len = max((len(v) for v in variants), default=1)
                            name_similarity = max(
                                (
                                    self._similarity(variant, norm_name)
                                    * (len(variant) / full_len) ** 0.5
                                    for variant in variants
                                ),
                                default=0.0,
                            )
                            # 文书类型（主题内容）先于主语（分类）排序：
                            # 清单原句 / 文书类型原样出现在文件名里
                            # （“检测方案”不算“施工方案”）的优先取；主语
                            # 首词在文件名里出现过的优先于只是字面相近的。
                            rank = (
                                self._doc_bonus(
                                    group.text,
                                    group.docs,
                                    norm_name,
                                    raw_names[id(file)],
                                ),
                                self._head_present(variants, norm_name),
                                name_similarity,
                                -len(norm_name),
                            )
                            if pick_rank is None or rank > pick_rank:
                                pick_rank = rank
                                pick_full = (
                                    file,
                                    min(
                                        1.0,
                                        max(
                                            name_similarity,
                                            self.FOLDER_GROUP_THRESHOLD,
                                        ),
                                    ),
                                )
                    if pick_full:
                        full[group_index] = pick_full
                    if pick_any is None and pick_full:
                        pick_any = pick_full
                    if pick_any:
                        any_hit[group_index] = pick_any

                if (len(full), len(any_hit)) > (len(best_full), len(best_any)):
                    best_folder = folder
                    best_full, best_any = full, any_hit
                if len(full) == len(groups):
                    break

            if not best_any:
                continue

            ordered = list(best_full.values()) + [
                value
                for group_index, value in sorted(
                    best_any.items(), key=lambda item: item[1][1], reverse=True
                )
                if group_index not in best_full
            ]
            picks: list[tuple[FileRecord, float]] = []
            seen_paths: set[str] = set()
            for file, similarity in ordered:
                if file.rel_path in seen_paths:
                    continue
                seen_paths.add(file.rel_path)
                picks.append((file, similarity))
            evidence = [
                Evidence(
                    rel_path=file.rel_path,
                    score=round(similarity, 3),
                    code_score=1.0,
                    token_score=round(similarity, 3),
                    char_score=round(similarity, 3),
                )
                for file, similarity in picks[:3]
            ]
            folder_name = best_folder[-1] if best_folder else cp.archive_content
            names = "；".join(file.name for file, _ in picks[:2])

            if best_full:
                result.state = STATE_MATCHED
                result.score = max(result.score, self.MATCH_THRESHOLD + 0.03)
                result.evidence = evidence
                found = [groups[gi].text for gi in best_full]
                missing = [
                    groups[gi].text for gi in range(len(groups)) if gi not in best_full
                ]
                detail = f"命中：{'、'.join(found[:2])}"
                if missing:
                    detail += f"；未单独找到：{'、'.join(missing[:2])}"
                result.reason = (
                    f"按分类号在文件夹“{folder_name}”内找到对应文件（{detail}）：{names}"
                )
            elif result.state == STATE_MISSING:
                result.state = STATE_SUSPECT
                result.score = max(result.score, self.SUSPECT_THRESHOLD + 0.05)
                result.reason = (
                    f"疑似：文件夹“{folder_name}”内找到相近文件"
                    f"（{len(best_any)}/{len(groups)} 个分句，"
                    f"文书类型未完全对应）：{names}"
                )
                if not result.evidence:
                    result.evidence = evidence
            else:
                existing = {item.rel_path for item in result.evidence}
                merged = list(result.evidence) + [
                    item for item in evidence if item.rel_path not in existing
                ]
                result.evidence = sorted(
                    merged, key=lambda item: item.score, reverse=True
                )[:3]

            result.auto_state = result.state
            result.auto_reason = result.reason

    def _term_hit(self, term: str, path: str) -> float:
        """关键词命中率。

        先看整词；整词不在时，按“词首片段”给部分分。不能用词尾，
        否则“砂使用跟踪记录表”会因尾部“使用跟踪记录表”命中钢筋资料，
        把主语丢掉。
        """
        if not term or not path:
            return 0.0
        if term in path:
            return 1.0
        size = len(term)
        if size < 4:
            return 0.0
        for length in range(min(size - 1, 16), 3, -1):
            if term[:length] in path:
                return length / size
        return 0.0

    @staticmethod
    def _part_in_name(part: str, name_text: str) -> bool:
        """并列条目的单个分句是否由文件名覆盖。

        “开工报审”可被“开工报告及报审”覆盖：文书词“报审”和主语“开工”
        都在文件名里。整句在文件名里出现则直接通过。没有文书词的
        “控制文件”这类分句必须整句出现，避免只命中前半段就补分。
        """
        cleaned = clean_for_match(part)
        if len(cleaned) < 2 or cleaned in STOPWORDS:
            return True
        if cleaned in name_text:
            return True
        docs = [term for term in DOC_TERMS if term in part]
        if not docs:
            return False
        subject = cleaned
        for term in docs:
            subject = subject.replace(clean_for_match(term), "")
        return bool(subject) and subject in name_text and any(
            clean_for_match(term) in name_text for term in docs
        )

    def _code_score(self, file_codes: list[str], cp_code: str) -> float:
        best = 0.0
        for code in file_codes:
            if not code or not cp_code:
                continue
            if code == cp_code:
                return 1.0
            if cp_code.startswith(code) or code.startswith(cp_code):
                best = max(best, 0.6)
        return best

    def _candidate_ids(self, file: FileRecord, path: str, path_3: set[str], path_2: set[str]) -> set[int]:
        candidates: set[int] = set()
        for gram in path_3:
            candidates.update(self.cp_index.get(gram, ()))
        for gram in path_2:
            candidates.update(self.cp_index.get(gram, ()))
        for code in file.codes:
            candidates.update(self.by_code.get(code, ()))
            for size in range(2, len(code) + 1):
                candidates.update(self.by_prefix.get(code[:size], ()))
        return candidates

    def _score(
        self,
        index: int,
        file_codes: list[str],
        path: str,
        path_3: set[str],
        name_text: str,
    ) -> Scored:
        cp = self.checkpoints[index]
        code_score = self._code_score(file_codes, cp.code)
        norm_path = self._normalize_text(path)
        norm_name = self._normalize_text(name_text)

        terms = self.cp_terms[index]
        norm_terms = self.cp_norm_terms[index]
        # 文书类型等价只在主语基本对上时才给分：否则一张“金具开箱检查记录”
        # 会靠“检查记录”顶替“检查及评级记录”。
        subject_gate = max(
            (
                self._term_hit(self._normalize_text(term), norm_name)
                for term in self.cp_subjects[index]
            ),
            default=0.0,
        ) >= 0.6
        total_weight = sum(self.idf.get(term, 1.0) for term in terms)
        matched_weight = 0.0
        for term, norm_term in zip(terms, norm_terms):
            hit = self._term_hit(term, path)
            # 同义词/换序只在归一化后能对上时加分，原词命中的结果不受影响。
            # 路径被换序（光缆OPGW→OPGW光缆）时，即使词本身没变也要按
            # 归一化路径再算一次。
            if norm_term != term or norm_path != path:
                hit = max(hit, self._term_hit(norm_term, norm_path))
            if hit < 1.0 and subject_gate:
                hit = max(hit, self._doc_family_hit(term, norm_name))
            matched_weight += self.idf.get(term, 1.0) * hit
        token_score = matched_weight / total_weight if total_weight else 0.0

        grams = self.cp_grams[index]
        char_score = len(grams & path_3) / len(grams) if grams else 0.0

        score = 0.20 * code_score + 0.45 * token_score + 0.35 * char_score
        boost = ""
        excluded = bool(
            cp.exclude_terms
            and any(clean_for_match(term) in path for term in cp.exclude_terms)
        )
        if excluded:
            score *= 0.15

        # 文书类型对不上（要“检测报告”却只有“检测方案/记录”）时降权。
        # 只看文件名判断文书类型是否真的存在：文件夹名罗列了“安全技术交底”
        # 不代表某份文件就是交底记录。
        cp_docs = self.cp_docs[index]
        doc_ok = self._doc_strict(tuple(cp_docs), norm_name) if cp_docs else True
        if cp_docs and not doc_ok and not any(term in path for term in cp_docs):
            score *= 0.80

        # 重复条目用归档内容消歧；类目完全不沾边时不判“已匹配”
        context_terms = self.cp_context[index]
        context_hit = any(term in path for term in context_terms)
        context_blocked = bool(
            self.cp_needs_context[index] and context_terms and not context_hit
        )
        if context_blocked:
            score *= 0.45
        elif context_hit:
            score = min(1.0, score + 0.03)

        # 文件路径带分类号、却与检查点不一致时，最多只到“疑似”，
        # 避免“设计变更与竣工图修改对照表”被当成竣工图。
        relevant_codes = [code for code in file_codes if code in self.known_prefixes]
        if relevant_codes and code_score == 0:
            score = min(score, self.MATCH_THRESHOLD - 0.001)

        # 只命中“试验报告/报审表”这类通用文书词、主语完全没出现时，
        # 最多判“疑似”。例如“地脚螺栓第三方试验报告”不能靠
        # “钢筋原材料检测试验报告及报审表”里的“试验报告”命中。
        subjects = self.cp_subjects[index]
        subject_hit = any(
            self._term_hit(term, norm_name) >= 0.6
            or (
                len(term) >= 4
                and self._filename_support(
                    self._normalize_text(term), norm_name, norm_path
                )
            )
            for term in subjects
        )
        subject_blocked = bool(subjects and not subject_hit)
        if subject_blocked:
            score = min(score, self.MATCH_THRESHOLD - 0.001)

        # 要的文书类型（交底/试验报告/验收记录…）在文件名里完全没有，
        # 同样不能判“已匹配”。文件夹名里的字样不能代替文件名。
        if cp_docs and not doc_ok:
            score = min(score, self.MATCH_THRESHOLD - 0.001)

        # 只命中分类编码、文字证据很弱的，不直接判“已匹配”。
        # 放在补分规则之前：补分规则自己有更强的文字证据要求，
        # 不能被这条基于整词覆盖率的弱证据门槛再打回去。
        if score >= self.MATCH_THRESHOLD and token_score < 0.18 and char_score < 0.30:
            score = self.MATCH_THRESHOLD - 0.001

        # 语序不同但要素齐全：清单写“开工报审及开工报告”，现场写
        # “开工报告及报审”。只对含“及/和/与/或”的并列条目启用，并且
        # 每个分句的文书类型、主语都必须由**文件名**覆盖——不能靠文件夹名，
        # 否则同一文件夹里“钢筋使用跟踪记录表”会被补成“钢筋进场到货记录”。
        # 任何一个降权条件成立都不补，避免把“用水试块”或“砂见证取样”
        # 硬拉到已匹配。补分只到阈值上方一点，仍属“中”置信度。
        groups = self.cp_groups[index]
        groups_ok = bool(groups) and all(
            self._part_in_name(part, name_text) for part in groups
        )
        if (
            groups_ok
            and not excluded
            and not context_blocked
            and not subject_blocked
            and code_score >= 1.0
            and token_score >= 0.5
            and cp_docs
            and all(term in name_text for term in cp_docs)
        ):
            score = max(score, self.MATCH_THRESHOLD + 0.03)
            boost = "连接词语序与清单不同，已按分句要素补分"

        # 换序/换词兜底：全部长词都能由文件名支撑才升级为已匹配；
        # 只支撑一部分的，最高抬到疑似并附候选文件，交人工确认。
        long_terms = [term for term in norm_terms if len(term) >= 4]
        if (
            long_terms
            and code_score >= 1.0
            and not excluded
            and not context_blocked
            and not subject_blocked
            and (not cp_docs or any(term in name_text for term in cp_docs))
        ):
            supported = [
                self._filename_support(term, norm_name, norm_path)
                for term in long_terms
            ]
            if all(supported):
                score = max(score, self.MATCH_THRESHOLD + 0.03)
                boost = boost or "用词/语序与清单不同，已按文件名要素锚点补分"
            elif any(supported) and score < self.MATCH_THRESHOLD:
                score = max(score, self.SUSPECT_THRESHOLD + 0.05)

        return Scored(index=index, score=score, code_score=code_score,
                      token_score=token_score, char_score=char_score,
                      boost=boost)

    def score_file(self, file: FileRecord) -> list[Scored]:
        path = self._path_text(file)
        if not path:
            return []
        path_3 = char_ngrams(path, 3)
        path_2 = char_ngrams(path, 2)
        candidates = self._candidate_ids(file, path, path_3, path_2)
        if not candidates:
            return []

        if len(candidates) > self.MAX_CANDIDATES:
            def rank(index: int) -> tuple[int, int]:
                code = self._code_score(file.codes, self.checkpoints[index].code)
                return (1 if code > 0 else 0, len(self.cp_grams[index] & path_3))
            candidates = set(sorted(candidates, key=rank, reverse=True)[: self.MAX_CANDIDATES])

        name_text = clean_for_match(file.name)
        scored = [
            self._score(index, file.codes, path, path_3, name_text)
            for index in candidates
        ]
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored

    def match(self, files: list[FileRecord], progress: ProgressFn | None = None) -> list[MatchResult]:
        self.set_file_context(files)
        best: dict[int, Scored] = {}
        evidence: dict[int, list[Evidence]] = defaultdict(list)
        total = len(files)

        for position, file in enumerate(files, start=1):
            scored = self.score_file(file)
            if scored:
                top = scored[0]
                file.best_score = top.score
                file.best_cp_id = self.checkpoints[top.index].id
                file.best_cp_text = self.checkpoints[top.index].text
                file.matched_cp_ids = [
                    self.checkpoints[item.index].id
                    for item in scored[:20]
                    if item.score >= self.SUSPECT_THRESHOLD
                ]
                for item in scored[: self.MAX_PER_FILE]:
                    if item.score < self.SUSPECT_THRESHOLD:
                        continue
                    bucket = evidence[item.index]
                    if len(bucket) < 3 or item.score > min(ev.score for ev in bucket):
                        bucket.append(
                            Evidence(
                                rel_path=file.rel_path,
                                score=item.score,
                                code_score=item.code_score,
                                token_score=item.token_score,
                                char_score=item.char_score,
                            )
                        )
                        bucket.sort(key=lambda ev: ev.score, reverse=True)
                        del bucket[3:]
                    if item.index not in best or item.score > best[item.index].score:
                        best[item.index] = item
            if progress and (position % 25 == 0 or position == total):
                progress(position, total)

        results: list[MatchResult] = []
        for index, cp in enumerate(self.checkpoints):
            item = best.get(index)
            score = item.score if item else 0.0
            evs = sorted(evidence.get(index, []), key=lambda ev: ev.score, reverse=True)[:3]

            if cp.optional:
                state = STATE_MATCHED if score >= self.MATCH_THRESHOLD else STATE_NA
            elif score >= self.MATCH_THRESHOLD:
                state = STATE_MATCHED
            elif score >= self.SUSPECT_THRESHOLD:
                state = STATE_SUSPECT
            else:
                state = STATE_MISSING

            if state == STATE_MATCHED and evs:
                first = evs[0]
                reason = (
                    f"命中 {first.rel_path}（编码 {first.code_score:.2f} / "
                    f"关键词 {first.token_score:.2f} / 文本 {first.char_score:.2f}）"
                )
                if item and item.boost:
                    reason += f"；{item.boost}，建议复核"
            elif state == STATE_SUSPECT and evs:
                reason = f"疑似 {evs[0].rel_path}，建议人工确认"
            elif state == STATE_NA:
                reason = cp.skip_reason or "其他/备查类条目，未提供时不判缺"
            else:
                reason = "未发现匹配文件"

            results.append(
                MatchResult(
                    checkpoint=cp,
                    state=state,
                    score=score,
                    evidence=evs,
                    reason=reason,
                    auto_state=state,
                    auto_reason=reason,
                )
            )

        # 全局打分漏掉的，再按分类号落到具体文件夹里做语义兜底
        self._folder_fallback(results, files)

        # 同一份文件支撑过多条目时提示“疑似合卷”
        path_counts = Counter(
            result.evidence[0].rel_path for result in results if result.evidence
        )
        for result in results:
            if result.evidence:
                count = path_counts.get(result.evidence[0].rel_path, 0)
                if count > 3:
                    result.shared = True
                    result.reason += f"；同一文件支撑 {count} 条，注意合卷核对"

        return results
