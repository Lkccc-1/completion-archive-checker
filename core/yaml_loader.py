"""YAML 检查点加载器。

支持两种格式：
1. 本工具导出的 canonical 格式（顶层 checkpoints）；
2. 早期手写的 groups/items 格式（checkpoints/附录C-基本建设.yaml）。
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .docx_parser import TemplateParseError
from .models import Checkpoint, CheckpointSet, Node, UNITS
from .text import extract_terms, is_optional_text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [str(value)]


def _normalize_retention(value: Any, owner: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        # 保留空值，保证导出后再加载的保管期限表结构与原表一致
        return {str(key): "" if val is None else str(val) for key, val in value.items()}
    if value:
        key = owner if owner in UNITS else "建设"
        return {key: str(value)}
    return {}


def _build_nodes_from_paths(checkpoints: list[Checkpoint]) -> list[Node]:
    """canonical YAML 没有 nodes 时，按 path 合成层级，供项目类型下拉使用。"""
    nodes: dict[str, Node] = {}
    order = 0
    for cp in checkpoints:
        path = cp.path or [cp.category]
        code = cp.code if cp.code.isdigit() else ""
        for depth in range(1, len(path) + 1):
            prefix_names = path[:depth]
            key = "|".join(prefix_names)
            if key in nodes:
                continue
            level = depth
            node_code = code[:level] if code and len(code) >= level else ""
            parent_key = "|".join(path[: depth - 1]) if depth > 1 else None
            nodes[key] = Node(
                key=key,
                code=node_code,
                name=path[depth - 1],
                level=level,
                parent_key=parent_key,
                order=order,
            )
            order += 1
    return sorted(nodes.values(), key=lambda item: item.order)


def _parse_canonical(data: dict[str, Any], path: Path) -> CheckpointSet:
    raw_checkpoints = data.get("checkpoints") or []
    if not isinstance(raw_checkpoints, list) or not raw_checkpoints:
        raise TemplateParseError("YAML 的 checkpoints 为空。")

    checkpoints: list[Checkpoint] = []
    for index, item in enumerate(raw_checkpoints):
        if not isinstance(item, dict):
            continue
        path_names = _as_list(item.get("path"))
        category = str(item.get("category") or (path_names[-1] if path_names else ""))
        code = str(item.get("code") or "")
        text = str(item.get("text") or item.get("name") or "").strip()
        if not text:
            continue
        match = item.get("match") if isinstance(item.get("match"), dict) else {}
        keywords = _as_list(item.get("keywords")) + _as_list(match.get("filename_any"))
        keywords = keywords or extract_terms(" ".join([text, category]))
        retention = _normalize_retention(item.get("retention"), str(item.get("filing") or ""))
        filing = str(item.get("filing") or item.get("owner") or "")
        source = str(item.get("source") or item.get("owner") or "")
        checkpoints.append(
            Checkpoint(
                id=str(item.get("id") or f"CP-{code or 'YAML'}-{index + 1:04d}"),
                node_key=str(item.get("node_key") or "|".join(path_names) or category),
                code=code,
                category=category,
                path=path_names or [category],
                archive_content=str(item.get("archive_content") or item.get("sub") or category),
                text=text,
                seq=int(item.get("seq") or index + 1),
                source=source,
                filing=filing,
                retention=retention,
                keywords=keywords,
                exclude_terms=_as_list(match.get("filename_exclude")),
                optional=bool(item.get("optional") or is_optional_text(text)),
                skip_reason=str(item.get("skip_reason") or ""),
                extra={"raw": item},
            )
        )

    if not checkpoints:
        raise TemplateParseError("YAML 中没有可用的检查点。")

    nodes = _build_nodes_from_paths(checkpoints)
    meta = dict(data.get("meta") or data.get("profile") or {})
    meta.update(
        {
            "schema_version": int(data.get("schema_version") or 2),
            "source_file": path.name,
            "source_hash": _sha256(path),
            "template_name": str(meta.get("name") or path.stem),
            "parsed_at": datetime.now().isoformat(timespec="seconds"),
            "doc_types": sorted({cp.code[0] for cp in checkpoints if cp.code.isdigit()}),
            "project_types": [
                {"code": node.code, "name": node.name}
                for node in nodes
                if node.level == 2
            ],
            "total_checkpoints": len(checkpoints),
            "total_nodes": len(nodes),
        }
    )
    return CheckpointSet(meta=meta, nodes=nodes, checkpoints=checkpoints, warnings=[])


def _parse_legacy(data: dict[str, Any], path: Path) -> CheckpointSet:
    groups = data.get("groups") or []
    if not isinstance(groups, list) or not groups:
        raise TemplateParseError("YAML 的 groups 为空。")

    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    checkpoints: list[Checkpoint] = []
    nodes: dict[str, Node] = {}
    order = 0
    warnings = ["YAML 使用旧版 groups/items 格式，已转换为统一检查点模型。"]

    def ensure_node(code: str, name: str, level: int, parent_key: str | None) -> Node:
        nonlocal order
        key = f"{parent_key or ''}|{code}|{name}"
        if key not in nodes:
            nodes[key] = Node(
                key=key, code=code, name=name, level=level, parent_key=parent_key, order=order
            )
            order += 1
        return nodes[key]

    for group in groups:
        if not isinstance(group, dict):
            continue
        code = str(group.get("code") or "")
        group_name = str(group.get("name") or code)
        owner = str(group.get("owner") or "")
        retention = _normalize_retention(group.get("retention"), owner)

        # 旧格式没有完整的 1/2 级节点，按编码前缀补出来
        parent_key: str | None = None
        if code.isdigit() and len(code) >= 3:
            doc_type = code[0]
            type_code = code[:2]
            type_name = str(profile.get("project_type") or type_code)
            root = ensure_node(doc_type, "基本建设" if doc_type == "8" else "设备仪器", 1, None)
            type_node = ensure_node(type_code, type_name, 2, root.key)
            parent_key = type_node.key

        group_node = ensure_node(code, group_name, len(code) if code.isdigit() else 3, parent_key)
        path_names = []
        cursor: Node | None = group_node
        while cursor:
            path_names.append(cursor.name)
            cursor = nodes.get(cursor.parent_key) if cursor.parent_key else None
        path_names.reverse()

        for index, item in enumerate(group.get("items") or []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("name") or "").strip()
            if not text:
                continue
            item_owner = str(item.get("owner") or owner)
            match = item.get("match") if isinstance(item.get("match"), dict) else {}
            keywords = _as_list(match.get("filename_any")) or extract_terms(
                " ".join([text, str(item.get("sub") or group_name)])
            )
            item_retention = _normalize_retention(item.get("retention"), item_owner) or retention
            checkpoints.append(
                Checkpoint(
                    id=str(item.get("id") or f"CP-{code}-{index + 1:04d}"),
                    node_key=group_node.key,
                    code=code,
                    category=group_name,
                    path=path_names,
                    archive_content=str(item.get("sub") or group_name),
                    text=text,
                    seq=index + 1,
                    source=item_owner,
                    filing=item_owner,
                    retention=item_retention,
                    keywords=keywords,
                    exclude_terms=_as_list(match.get("filename_exclude")),
                    optional=bool(is_optional_text(text)),
                    extra={"checks": item.get("checks")},
                )
            )

    if not checkpoints:
        raise TemplateParseError("YAML 中没有可用的检查点。")

    meta = dict(profile)
    meta.update(
        {
            "schema_version": int(data.get("schema_version") or 1),
            "source_file": path.name,
            "source_hash": _sha256(path),
            "template_name": str(profile.get("name") or path.stem),
            "parsed_at": datetime.now().isoformat(timespec="seconds"),
            "doc_types": sorted({cp.code[0] for cp in checkpoints if cp.code.isdigit()}),
            "project_types": [
                {"code": node.code, "name": node.name}
                for node in nodes.values()
                if node.level == 2
            ],
            "total_checkpoints": len(checkpoints),
            "total_nodes": len(nodes),
        }
    )
    return CheckpointSet(
        meta=meta,
        nodes=sorted(nodes.values(), key=lambda item: item.order),
        checkpoints=checkpoints,
        warnings=warnings,
    )


def parse_yaml(path: str | Path) -> CheckpointSet:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TemplateParseError(f"无法读取 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise TemplateParseError("YAML 顶层必须是对象。")
    if "checkpoints" in data:
        return _parse_canonical(data, path)
    if "groups" in data:
        return _parse_legacy(data, path)
    raise TemplateParseError("YAML 缺少 checkpoints 或 groups，无法识别格式。")


def dump_checkpoint_set(checkpoint_set: CheckpointSet) -> str:
    """导出为可再次加载的 canonical YAML。"""
    data = {
        "schema_version": 2,
        "meta": checkpoint_set.meta,
        "nodes": [asdict(node) for node in checkpoint_set.nodes],
        "checkpoints": [
            {
                "id": cp.id,
                "node_key": cp.node_key,
                "code": cp.code,
                "category": cp.category,
                "path": cp.path,
                "archive_content": cp.archive_content,
                "text": cp.text,
                "seq": cp.seq,
                "source": cp.source,
                "filing": cp.filing,
                "retention": cp.retention,
                "keywords": cp.keywords,
                "optional": cp.optional,
                "skip_reason": cp.skip_reason,
                "match": {"filename_exclude": cp.exclude_terms},
            }
            for cp in checkpoint_set.checkpoints
        ],
        "warnings": checkpoint_set.warnings,
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
