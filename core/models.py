"""领域模型：检查点、文件记录、匹配结果、检查报告。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .text import split_units

DOC_TYPES = {"8": "基本建设", "9": "设备仪器"}
UNITS = ("建设", "设计", "监理", "施工")

STATE_MATCHED = "已匹配"
STATE_SUSPECT = "疑似"
STATE_MISSING = "缺失"
STATE_NA = "不适用"


@dataclass
class Node:
    key: str
    code: str
    name: str
    level: int
    parent_key: str | None
    order: int


@dataclass
class Checkpoint:
    id: str
    node_key: str
    code: str
    category: str
    path: list[str]
    archive_content: str
    text: str
    seq: int
    source: str
    filing: str
    retention: dict[str, str]
    keywords: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    optional: bool = False
    skip_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def filing_units(self) -> list[str]:
        return split_units(self.filing)

    @property
    def retention_text(self) -> str:
        return " / ".join(f"{unit}{value}" for unit, value in self.retention.items() if value)

    @property
    def display_path(self) -> str:
        return " > ".join(self.path)


@dataclass
class CheckpointSet:
    meta: dict[str, Any]
    nodes: list[Node]
    checkpoints: list[Checkpoint]
    warnings: list[str] = field(default_factory=list)

    def node_by_key(self, key: str | None) -> Node | None:
        for node in self.nodes:
            if node.key == key:
                return node
        return None

    def project_types(self, doc_type: str | None = None) -> list[Node]:
        return [
            node
            for node in self.nodes
            if node.level == 2 and (doc_type is None or node.code.startswith(doc_type))
        ]


@dataclass
class FileRecord:
    rel_path: str
    dir_parts: list[str]
    name: str
    stem: str
    ext: str
    size: int
    mtime: float
    codes: list[str] = field(default_factory=list)
    content_text: str | None = None
    best_score: float = 0.0
    best_cp_id: str = ""
    best_cp_text: str = ""
    matched_cp_ids: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    rel_path: str
    score: float
    code_score: float = 0.0
    token_score: float = 0.0
    char_score: float = 0.0

    @property
    def short(self) -> str:
        return f"{self.rel_path}（{self.score:.2f}）"


@dataclass
class MatchResult:
    checkpoint: Checkpoint
    state: str
    score: float
    evidence: list[Evidence] = field(default_factory=list)
    reason: str = ""
    shared: bool = False
    manual: bool = False
    note: str = ""
    # 自动判定快照：人工纠正被撤销时用它恢复，避免状态残留
    auto_state: str = ""
    auto_reason: str = ""
    # 正文证据（读 PDF/扫描件得到）
    content_score: float = 0.0
    content_pages: int = 0
    content_ocr: bool = False
    content_snippet: str = ""
    content_confirmed: bool = False
    # 印章核查
    needs_seal: bool = False
    seal_state: str = ""
    seal_pages: list[int] = field(default_factory=list)
    seal_count: int = 0
    # 大模型复判：只是建议，人工采纳后才走 OverrideStore 改判
    llm_verdict: str = ""
    llm_reason: str = ""
    llm_quote: str = ""
    llm_confidence: float = 0.0
    llm_cached: bool = False
    llm_error: str = ""

    @property
    def evidence_text(self) -> str:
        return "；".join(item.short for item in self.evidence)

    @property
    def seal_text(self) -> str:
        if not self.needs_seal:
            return ""
        if self.seal_state == "有":
            where = "、".join(f"第{p}页" for p in self.seal_pages[:3])
            return f"有（{self.seal_count} 处：{where}）" if self.seal_count else "有"
        if self.seal_state == "无":
            return "未检出"
        if self.seal_state == "未知":
            return "不可判"
        return ""

    @property
    def content_text(self) -> str:
        if not self.content_pages:
            return "未读正文"
        mark = "OCR" if self.content_ocr else "文字层"
        return f"{self.content_score:.2f}（{self.content_pages}页·{mark}）"

    @property
    def llm_text(self) -> str:
        if not self.llm_verdict:
            return "未复判" if not self.llm_error else "复判失败"
        label = {"match": "建议已提供", "gap": "建议真缺", "unclear": "无法判断"}.get(
            self.llm_verdict, "无法判断"
        )
        if self.llm_confidence:
            label = f"{label}（{self.llm_confidence:.2f}）"
        if self.llm_cached:
            label += "·缓存"
        return label

    @property
    def confidence(self) -> str:
        if self.score >= 0.8:
            return "高"
        if self.score >= 0.62:
            return "中"
        if self.score >= 0.4:
            return "低"
        return "—"


@dataclass
class CategoryGap:
    code: str
    category: str
    path: list[str]
    count: int


@dataclass
class CheckReport:
    meta: dict[str, Any]
    results: list[MatchResult]
    category_gaps: list[CategoryGap]
    orphan_files: list[dict[str, Any]]
    stats: dict[str, Any]

    def by_state(self, *states: str) -> list[MatchResult]:
        wanted = set(states)
        return [item for item in self.results if item.state in wanted]
