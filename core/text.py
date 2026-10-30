"""文本归一化、拆条与关键词抽取。

匹配器同时依赖两路文本信号：
- 关键词覆盖率：从检查点文字里抽出的业务词（含文书类型）是否出现在文件路径里；
- 字符 n-gram 覆盖率：解决“关键词被拆开/写法略有差异”时的召回。
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Iterable

# 逐条拆分后的条目，若包含这些短语，多半是类目说明而不是具体文件
SKIP_PHRASES = (
    "涉及两个以上分类号",
    "综合或无法归入",
    "无法归入其他分类号",
)

OPTIONAL_MARKERS = ("其他", "其它", "备查", "参考")

# 只在术语完全相等时剔除；像“施工日志”这种组合词会保留
STOPWORDS = {
    "工程", "项目", "文件", "资料", "记录", "报告", "表格", "报表", "报审", "报审表",
    "及", "等", "有关", "相关", "其他", "其它", "附件", "单位", "建设", "设计", "监理", "施工",
    "齐全", "完整", "主要", "内容", "情况", "工作", "管理", "要求", "规定", "标准",
    "规范", "说明", "证明", "材料", "进行", "以及", "或者", "包括", "如下", "以上",
    "以下", "部分", "全部", "根据", "按照", "执行", "过程",
}

# 常见文书类型：从长条目里再抽出“主题 + 文书类型”，避免长句无法整串命中
DOC_TERMS = (
    "质量证明", "试验报告", "检测报告", "验收记录", "检查记录", "开工报告", "竣工报告",
    "报审表", "报验表", "申请表", "汇总表", "明细表", "统计表", "通知书", "说明书",
    "竣工图", "装箱单", "合格证", "承诺书", "委托书", "任命书", "许可证", "合同",
    "协议", "批复", "请示", "报告", "记录", "台账", "清单", "方案", "细则", "大纲",
    "证书", "证明", "图纸", "照片", "影像", "纪要", "日志", "总结", "计划", "预算",
    "决算", "结算", "签证", "发票", "收据", "交底", "意见", "名册", "花名册",
    "验收表", "评定表", "划分表", "报审", "审批", "申请", "备案", "移交",
)

_SERIAL_RE = re.compile(r"^\s*(?:\d{1,2})(?!\d)\s*[.、．)）]?\s*")
_CN_SERIAL_RE = re.compile(r"^\s*[一二三四五六七八九十]{1,3}\s*[、.．)）]\s*")
_TERM_SPLIT_RE = re.compile(r"[、，,；;：:（）()《》【】\[\]/\s]+")
_UNIT_SPLIT_RE = re.compile(r"[\\/、,，;；\n\r\t ]+")
_CODE_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")


def nfkc(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "")


def join_lines(value: str | None, sep: str = "") -> str:
    parts = [p.strip() for p in re.split(r"[\r\n]+", value or "") if p.strip()]
    return sep.join(parts)


def clean_display(value: str | None) -> str:
    return re.sub(r"[ \t]+", " ", nfkc(value)).strip()


def clean_for_match(value: str | None) -> str:
    text = nfkc(value).lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[（）()【】\[\]《》<>]", "", text)
    text = re.sub(r"[、，,；;：:。．.·\-—_/\\|]+", "", text)
    return text


def strip_serial(value: str | None) -> str:
    """去掉行首序号（“4光传输”“1.开工报告”）。

    匹配用 NFKC 归一化后的文本，但切回原文取剩余部分，避免把
    “（3）施工日志”这类展示文本的半角括号一起改掉。
    """
    raw = (value or "").lstrip()
    text = nfkc(raw)
    match = _CN_SERIAL_RE.match(text) or _SERIAL_RE.match(text)
    if not match:
        return raw.strip()
    if len(text) == len(raw):
        return raw[match.end():].strip()
    stripped = _CN_SERIAL_RE.sub("", text, count=1)
    return _SERIAL_RE.sub("", stripped, count=1).strip()


def parse_seq(value: str | None, fallback: int = 0) -> int:
    text = nfkc(value).lstrip()
    m = re.match(r"^(\d{1,2})(?!\d)", text)
    if m:
        return int(m.group(1))
    return fallback


def split_units(value: str | None) -> list[str]:
    """把“建设/物资供应”“设计//施工”拆成单位列表。"""
    if not value:
        return []
    out: list[str] = []
    for part in _UNIT_SPLIT_RE.split(nfkc(value)):
        part = part.strip().strip("等")
        if part and part not in out:
            out.append(part)
    return out


def extract_codes(value: str | None) -> list[str]:
    """提取 3~4 位分类编码，排除 1900~2100 的年份。"""
    codes: list[str] = []
    for code in _CODE_RE.findall(nfkc(value)):
        if 1900 <= int(code) <= 2100:
            continue
        if code not in codes:
            codes.append(code)
    return codes


def split_bullets(value: str | None) -> tuple[list[tuple[str, str]], list[str]]:
    """把“主要项目文件”单元格拆成检查点。

    返回 (条目列表, 被跳过的说明性文字)。条目为 (清洗后文本, 原始行)。
    """
    lines = [line.strip() for line in re.split(r"[\r\n]+", value or "") if line.strip()]
    bullets: list[tuple[str, str]] = []
    context: list[str] = []
    for line in lines:
        if line.endswith(("：", ":")):
            context.append(line)
            continue
        if any(phrase in line for phrase in SKIP_PHRASES):
            context.append(line)
            continue
        cleaned = strip_serial(line)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) < 2:
            continue
        bullets.append((cleaned, line))
    return bullets, context


def is_optional_text(value: str | None) -> bool:
    return any(marker in (value or "") for marker in OPTIONAL_MARKERS)


def extract_terms(value: str | None, limit: int = 60) -> list[str]:
    """抽取业务词：整段主题词 + 文书类型 + 去掉文书类型后的主题。"""
    text = strip_serial(nfkc(value))
    raw_parts = _TERM_SPLIT_RE.split(text)
    terms: list[str] = []

    def add(term: str) -> None:
        term = term.strip("。．.·-—_ ").strip()
        if len(term) >= 2 and term not in STOPWORDS and term not in terms:
            terms.append(term)

    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        # 先按“及/和/与/或”拆开：“开工报审及开工报告”拆成两个都能命中的词
        subparts = [p.strip() for p in re.split(r"(?<=.)[及和与或](?=.)", part) if p.strip()]
        for subpart in subparts or [part]:
            add(subpart)
            for doc_term in DOC_TERMS:
                if doc_term not in subpart:
                    continue
                add(doc_term)
                subject = subpart.replace(doc_term, "")
                subject = re.sub(r"^[及和与的]+|[及和与的]+$", "", subject)
                add(subject)
    return terms[:limit]


def char_ngrams(value: str | None, n: int = 3) -> set[str]:
    text = clean_for_match(value)
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def detect_common_prefix(values: Iterable[str]) -> str:
    """识别文件名里反复出现的项目名，匹配前剔除，降低噪声。"""
    cleaned = [strip_serial(v) for v in values if v]
    if len(cleaned) < 3:
        return ""
    prefix = os.path.commonprefix(cleaned)
    return prefix if len(prefix) >= 8 else ""


def safe_excel_text(value: object) -> object:
    """防止 Excel 公式注入。"""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
