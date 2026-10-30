from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.models import Checkpoint, CheckpointSet, FileRecord, Node
from core.text import extract_codes

TEMPLATE = BASE / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx"
SAMPLE_DIR = BASE / "项目C（220kV输电线路工程）"
LEGACY_YAML = BASE / "checkpoints" / "附录C-基本建设.yaml"


def make_checkpoint(
    text: str,
    *,
    code: str = "8232",
    archive: str = "01基础工程",
    category: str = "基础",
    filing: str = "施工",
    optional: bool = False,
    seq: int = 1,
    keywords: list[str] | None = None,
    exclude_terms: list[str] | None = None,
) -> Checkpoint:
    return Checkpoint(
        id=f"CP-{code}-{seq:04d}",
        node_key=f"|{code}|{category}",
        code=code,
        category=category,
        path=["基本建设", "交流输电线路", "项目施工", category],
        archive_content=archive,
        text=text,
        seq=seq,
        source=filing,
        filing=filing,
        retention={"建设": "永久", "施工": "30年"},
        keywords=keywords or [],
        exclude_terms=exclude_terms or [],
        optional=optional,
    )


def make_file(rel_path: str, *, codes: list[str] | None = None) -> FileRecord:
    name = rel_path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    parts = rel_path.split("/")[:-1]
    return FileRecord(
        rel_path=rel_path,
        dir_parts=parts,
        name=name,
        stem=stem,
        ext="." + name.rsplit(".", 1)[-1].lower() if "." in name else "",
        size=1024,
        mtime=0.0,
        codes=codes if codes is not None else extract_codes(rel_path),
    )


def make_set(checkpoints: list[Checkpoint], *, doc_type: str = "8",
             project_type: str = "82") -> CheckpointSet:
    nodes = [
        Node(key="root", code=doc_type, name="基本建设", level=1, parent_key=None, order=0),
        Node(key="type", code=project_type, name="交流输电线路", level=2,
             parent_key="root", order=1),
    ]
    return CheckpointSet(
        meta={
            "template_name": "测试模版",
            "source_hash": "testhash",
            "schema_version": 2,
            "doc_types": [doc_type],
            "total_checkpoints": len(checkpoints),
            "total_nodes": len(nodes),
        },
        nodes=nodes,
        checkpoints=checkpoints,
    )
