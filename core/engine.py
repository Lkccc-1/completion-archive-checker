"""检查引擎：类型/单位过滤、匹配、整类缺失聚合、人工纠正。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Callable

from .matcher import Matcher, ProgressFn
from .models import (
    DOC_TYPES,
    STATE_MATCHED,
    STATE_MISSING,
    STATE_NA,
    STATE_SUSPECT,
    UNITS,
    CategoryGap,
    CheckReport,
    Checkpoint,
    CheckpointSet,
    FileRecord,
    MatchResult,
)


def filter_checkpoints(
    checkpoints: list[Checkpoint],
    doc_type: str,
    project_type: str,
    unit: str,
    include_retention: bool = False,
) -> list[Checkpoint]:
    out: list[Checkpoint] = []
    for cp in checkpoints:
        if doc_type and not cp.code.startswith(doc_type):
            continue
        if project_type and not cp.code.startswith(project_type):
            continue
        units = set(cp.filing_units)
        selected = not unit or unit in units or "相关" in units
        if not selected and include_retention and cp.retention.get(unit):
            selected = True
        if selected:
            out.append(cp)
    return out


def detect_project_types(
    files: list[FileRecord], checkpoint_set: CheckpointSet, doc_type: str
) -> list[tuple[str, int]]:
    known = {
        node.code
        for node in checkpoint_set.nodes
        if node.level == 2 and node.code.startswith(doc_type)
    }
    counts: Counter[str] = Counter()
    for file in files:
        for code in file.codes:
            if len(code) >= 3 and code[:2] in known:
                counts[code[:2]] += 1
    return counts.most_common()


def resolve_project_type(
    files: list[FileRecord], checkpoint_set: CheckpointSet, doc_type: str, project_type: str
) -> tuple[str, list[tuple[str, int]]]:
    detected = detect_project_types(files, checkpoint_set, doc_type)
    if project_type and project_type != "auto":
        return project_type, detected
    if detected:
        return detected[0][0], detected
    raise ValueError(
        "未能从文件夹编码自动识别项目类型（如 8232 这类 3~4 位编码）。"
        "请手动选择项目类型，或确认资料目录中是否保留了分类号。"
    )


def apply_overrides(results: list[MatchResult], overrides: dict[str, dict[str, Any]]) -> None:
    state_map = {
        "已匹配": STATE_MATCHED,
        "缺失": STATE_MISSING,
        "不适用": STATE_NA,
        "疑似": STATE_SUSPECT,
        "已提供": STATE_MATCHED,
        "已提供（改判已匹配）": STATE_MATCHED,
        "确认缺失": STATE_MISSING,
    }
    for result in results:
        override = overrides.get(result.checkpoint.id)
        if not override:
            continue
        state = state_map.get(str(override.get("state") or ""))
        if state:
            result.state = state
        result.note = str(override.get("note") or "")
        result.manual = True
        if result.note:
            result.reason = f"人工纠正：{result.note}"
        elif state:
            result.reason = f"人工纠正为“{state}”"


def build_category_gaps(results: list[MatchResult]) -> list[CategoryGap]:
    all_groups: dict[str, list[MatchResult]] = defaultdict(list)
    for result in results:
        code = result.checkpoint.code
        prefix = code[:3] if len(code) >= 3 else code
        all_groups[prefix].append(result)

    covered_prefixes: set[str] = set()
    gaps: list[CategoryGap] = []

    for prefix, items in all_groups.items():
        if len(items) >= 3 and all(item.state == STATE_MISSING for item in items):
            first = items[0].checkpoint
            gaps.append(
                CategoryGap(
                    code=prefix,
                    category=_category_name(results, prefix, first),
                    path=first.path,
                    count=len(items),
                )
            )
            covered_prefixes.add(prefix)

    leaf_groups: dict[str, list[MatchResult]] = defaultdict(list)
    for result in results:
        code = result.checkpoint.code
        prefix = code[:3] if len(code) >= 3 else code
        if prefix in covered_prefixes:
            continue
        leaf_groups[result.checkpoint.node_key].append(result)

    for items in leaf_groups.values():
        if len(items) < 2 or not all(item.state == STATE_MISSING for item in items):
            continue
        first = items[0].checkpoint
        gaps.append(
            CategoryGap(
                code=first.code,
                category=first.category,
                path=first.path,
                count=len(items),
            )
        )

    return sorted(gaps, key=lambda item: (item.code, -item.count))


def _group_name(cp: Checkpoint, prefix: str) -> str:
    if cp.code == prefix:
        return cp.category
    if len(cp.path) >= 2:
        return cp.path[-2]
    return cp.category


def _category_name(results: list[MatchResult], prefix: str, fallback: Checkpoint) -> str:
    return _group_name(fallback, prefix)


def build_stats(results: list[MatchResult]) -> dict[str, Any]:
    counter = Counter(result.state for result in results)
    total = len(results)
    na = counter[STATE_NA]
    denominator = max(1, total - na)
    by_category: dict[str, dict[str, Any]] = {}
    for result in results:
        cp = result.checkpoint
        prefix = cp.code[:3] if len(cp.code) >= 3 else cp.code
        item = by_category.setdefault(
            prefix,
            {
                "code": prefix,
                "category": _group_name(cp, prefix),
                "total": 0,
                "matched": 0,
                "suspect": 0,
                "missing": 0,
                "na": 0,
            },
        )
        item["total"] += 1
        if result.state == STATE_MATCHED:
            item["matched"] += 1
        elif result.state == STATE_SUSPECT:
            item["suspect"] += 1
        elif result.state == STATE_MISSING:
            item["missing"] += 1
        else:
            item["na"] += 1

    categories = []
    for item in by_category.values():
        denom = max(1, item["total"] - item["na"])
        item["completeness"] = item["matched"] / denom
        categories.append(item)
    categories.sort(key=lambda item: item["code"])

    return {
        "total": total,
        "matched": counter[STATE_MATCHED],
        "suspect": counter[STATE_SUSPECT],
        "missing": counter[STATE_MISSING],
        "na": counter[STATE_NA],
        "manual": sum(1 for result in results if result.manual),
        "completeness": counter[STATE_MATCHED] / denominator,
        "by_category": categories,
    }


def build_orphans(files: list[FileRecord], threshold: float) -> list[dict[str, Any]]:
    rows = []
    for file in files:
        if file.best_score >= threshold:
            continue
        rows.append(
            {
                "rel_path": file.rel_path,
                "ext": file.ext,
                "size": file.size,
                "best_score": file.best_score,
                "best_cp_id": file.best_cp_id,
                "best_cp_text": file.best_cp_text,
                "note": "",
            }
        )
    rows.sort(key=lambda item: item["rel_path"])
    return rows


def add_inventory_surplus(
    orphans: list[dict[str, Any]],
    files: list[FileRecord],
    results: list[MatchResult],
    doc_type: str,
    project_type: str,
) -> list[dict[str, Any]]:
    """文件清单标准下，列出“目录里有、清单里没有”的文件。

    普通标准的未归类只按分数阈值挑（低于 0.40）；清单是逐份列举的，
    多出来的文件常常长得像清单里的某一份（0.5~0.6 分），按分数挑不出来。
    这里改成“没有被任何检查点当作证据用过”即算清单外，但只看本次
    资料类型 / 项目类型范围内的编码，免得把 9x 那部分误报。

    加了两道闸：证据每条只留前三份（同类文件名极像时，排第四的会被挤出去、
    它其实对得上清单），所以“最高分已到匹配阈值”的文件不算清单外；
    被挤出去的文件本来就对得上，只是没被展示。
    """
    used = {
        item.rel_path
        for result in results
        if result.state != STATE_MISSING
        for item in result.evidence
    }
    seen = {item["rel_path"] for item in orphans}
    extra: list[dict[str, Any]] = []
    for file in files:
        if file.rel_path in used or file.rel_path in seen:
            continue
        if file.best_score >= Matcher.MATCH_THRESHOLD:
            continue
        in_scope = any(
            code.startswith(doc_type) and code.startswith(project_type)
            for code in file.codes
        )
        if not in_scope:
            continue
        extra.append(
            {
                "rel_path": file.rel_path,
                "ext": file.ext,
                "size": file.size,
                "best_score": file.best_score,
                "best_cp_id": file.best_cp_id,
                "best_cp_text": file.best_cp_text,
                "note": "清单里没有：目录里有这份文件，但没有被任何检查点用上",
            }
        )
    rows = orphans + extra
    rows.sort(key=lambda item: item["rel_path"])
    return rows


def content_available() -> tuple[bool, str]:
    """读正文/查印章的依赖是否就绪（pymupdf + opencv + numpy）。"""
    try:
        import numpy  # noqa: F401
        import cv2  # noqa: F401
        import pymupdf  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"缺少读 PDF 的依赖：{exc}"
    return True, ""


def check(
    checkpoint_set: CheckpointSet,
    files: list[FileRecord],
    doc_type: str,
    project_type: str,
    unit: str,
    *,
    include_retention: bool = False,
    overrides: dict[str, dict[str, Any]] | None = None,
    project_name: str = "",
    root: str = "",
    progress: ProgressFn | None = None,
    content: dict[str, Any] | None = None,
    content_progress: ProgressFn | None = None,
) -> CheckReport:
    resolved_type, detected = resolve_project_type(files, checkpoint_set, doc_type, project_type)
    filtered = filter_checkpoints(
        checkpoint_set.checkpoints, doc_type, resolved_type, unit, include_retention
    )
    if not filtered:
        raise ValueError(
            f"当前筛选条件下没有检查点：资料类型 {DOC_TYPES.get(doc_type, doc_type)}、"
            f"项目类型 {resolved_type}、单位 {unit}。请调整筛选条件。"
        )

    matcher = Matcher(
        filtered,
        project_name=project_name,
        known_codes=[cp.code for cp in checkpoint_set.checkpoints],
    )
    results = matcher.match(files, progress=progress)

    content_stats: dict[str, Any] = {"enabled": False}
    options = content or {}
    if options.get("enabled") and root:
        try:
            from .content import enrich_with_content
            from .pdf_reader import ContentReader

            reader = ContentReader(
                use_ocr=bool(options.get("use_ocr", True)),
                max_pages=int(options.get("max_pages", 3)),
                check_seal=bool(options.get("check_seal", True)),
                cache_path=options.get("cache_path"),
            )
            stats_obj = enrich_with_content(
                results,
                files,
                root,
                reader,
                max_files=int(options.get("max_files", 80)),
                check_seal=bool(options.get("check_seal", True)),
                deep=bool(options.get("deep", False)),
                deep_max_files=int(options.get("deep_max_files", 40)),
                respect_ids=set(overrides or {}),
                progress=content_progress,
            )
            reader.save()
            content_stats = {"enabled": True, **stats_obj.to_dict()}
        except Exception as exc:  # noqa: BLE001
            content_stats = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}

    if overrides:
        apply_overrides(results, overrides)

    gaps = build_category_gaps(results)
    stats = build_stats(results)
    orphans = build_orphans(files, matcher.SUSPECT_THRESHOLD)
    # 正文救回的文件已经作为证据用上了，不该再出现在“未归类”里
    used = {
        item.rel_path
        for result in results
        if result.state != STATE_MISSING
        for item in result.evidence
    }
    orphans = [item for item in orphans if item["rel_path"] not in used]
    if checkpoint_set.meta.get("mode") == "inventory":
        orphans = add_inventory_surplus(
            orphans, files, results, doc_type, resolved_type
        )

    type_name = resolved_type
    for node in checkpoint_set.project_types(doc_type):
        if node.code == resolved_type:
            type_name = f"{node.code} {node.name}"
            break

    meta: dict[str, Any] = {
        "project_name": project_name or root,
        "root": root,
        "doc_type": doc_type,
        "doc_type_name": DOC_TYPES.get(doc_type, doc_type),
        "project_type": resolved_type,
        "project_type_name": type_name,
        "unit": unit,
        "include_retention": include_retention,
        "template_name": checkpoint_set.meta.get("template_name", ""),
        "template_hash": checkpoint_set.meta.get("source_hash", ""),
        "template_schema": checkpoint_set.meta.get("schema_version", 2),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "filtered_total": len(filtered),
        "related_count": sum(1 for cp in filtered if "相关" in cp.filing_units),
        "detected_types": [{"code": code, "count": count} for code, count in detected],
        "match_threshold": matcher.MATCH_THRESHOLD,
        "suspect_threshold": matcher.SUSPECT_THRESHOLD,
        "units": list(UNITS),
        "content": content_stats,
    }

    return CheckReport(meta=meta, results=results, category_gaps=gaps,
                       orphan_files=orphans, stats=stats)


def reapply_overrides(
    report: CheckReport, overrides: dict[str, dict[str, Any]]
) -> CheckReport:
    """人工纠正后不重跑匹配，只重算状态、整类缺失和统计。"""
    for result in report.results:
        result.manual = False
        result.note = ""
        if result.auto_state:
            result.state = result.auto_state
            result.reason = result.auto_reason
    apply_overrides(report.results, overrides)
    return CheckReport(
        meta=report.meta,
        results=report.results,
        category_gaps=build_category_gaps(report.results),
        orphan_files=report.orphan_files,
        stats=build_stats(report.results),
    )
