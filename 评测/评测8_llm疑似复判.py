"""评测 8：大模型复判能不能把「真缺」和「口径差异」分开。

金标准是人工复核过的 15 条（复核-15条-已抽取.xlsx，含真实文件名，只留本地）。
每条都构造一份「工具判缺失」的情形，交给大模型复判：
  人工归类 = 真缺   → 期望 LLM 给 gap
  人工归类 = 口径差异 → 期望 LLM 给 match
其余归类（待定 / 系统漏判）不参与计分，只列出来看。

用法（key 走环境变量，不落盘）：
    set LLM_API_KEY=sk-...          # Windows cmd
    export LLM_API_KEY=sk-...       # bash
    python 评测/评测8_llm疑似复判.py --gold 复核-15条-已抽取.xlsx --root "…扫描件"

输出：控制台混淆表 + 评测/llm复判结果.csv（含真实路径，**不要**提交）。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import openpyxl  # noqa: E402

from core.llm_judge import (  # noqa: E402
    JudgeRequest,
    LLMConfig,
    LLMJudge,
    VERDICT_LABELS,
    case_index,
    case_list,
    excerpt_for,
    judge_request,
    nearby_names,
    related_folders,
    related_names,
)
from core.models import Checkpoint, MatchResult  # noqa: E402
from core.pdf_reader import ContentReader  # noqa: E402
from core.scanner import scan_directory  # noqa: E402
from core.template_loader import load_template  # noqa: E402
from core.text import clean_for_match  # noqa: E402

TEMPLATE = "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx"

EXPECT = {"真缺": "gap", "口径差异": "match"}
PDF_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def split_paths(cell: str) -> list[Path]:
    """单元格里一行一个路径。

    人工填表时常常只写了个近似文件名（少了 .pdf、或截断了后缀），这种情况
    文件本身不存在、但所在目录还在——保留它，后面按目录展开找候选。
    """
    parts = []
    for chunk in str(cell or "").replace("；", "\n").replace(";", "\n").splitlines():
        chunk = chunk.strip().strip('"')
        if not chunk:
            continue
        path = Path(chunk)
        if path.exists() or path.parent.exists():
            parts.append(path)
    return parts


def expand_candidates(paths: list[Path], limit: int = 8) -> list[Path]:
    """单元格里可能给的是文件夹、或写错后缀的文件名：统一展开成里面的文件。"""
    out: list[Path] = []
    for path in paths:
        if path.is_file():
            out.append(path)
        else:
            folder = path if path.is_dir() else path.parent
            if not folder.is_dir():
                continue
            for child in sorted(folder.iterdir()):
                if child.is_file() and child.suffix.lower() in PDF_SUFFIXES:
                    out.append(child)
                if len(out) >= limit:
                    return out
        if len(out) >= limit:
            break
    return out


def load_real_checkpoints() -> list:
    """从真实归档范围表取检查点。

    **必须用真表**：立卷单位、保管期限这些元数据是人工判「口径差异」的主要依据，
    自己捏一份假的（比如把立卷单位一律写成施工）等于给模型喂错误前提。
    """
    path = BASE / TEMPLATE
    if not path.exists():
        print(f"没找到归档范围表，退回构造检查点：{TEMPLATE}")
        return []
    return load_template(path).checkpoints


def pick_checkpoint(checkpoints, code: str, text: str):
    import difflib

    best = None
    for cp in checkpoints:
        if str(cp.code).strip() != code:
            continue
        ratio = difflib.SequenceMatcher(
            None, clean_for_match(cp.text), clean_for_match(text)
        ).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, cp)
    return best[1] if best and best[0] >= 0.5 else None


def infer_root(records: list[dict]) -> Path | None:
    """不传 --root 时，从单元格里给的文件路径反推资料根目录（它们的共同上级）。"""
    paths = []
    for record in records:
        for path in split_paths(record.get("files", "")):
            paths.append(path if path.is_dir() else path.parent)
            if not paths[-1].exists():
                paths[-1] = paths[-1].parent
    if not paths:
        return None
    try:
        common = Path(os.path.commonpath([str(path) for path in paths]))
    except ValueError:
        return None
    return common


def read_excerpt(files: list[Path], reader: ContentReader, limit: int = 800) -> str:
    """读候选文件的正文；图纸走 excerpt_for，优先取图签上的图名。"""
    for path in files:
        try:
            document = reader.read(path, path.name)
        except Exception:  # noqa: BLE001
            continue
        if not document.error and len(document.text.strip()) >= 20:
            return excerpt_for(document.text, limit)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default="复核-15条-已抽取.xlsx")
    parser.add_argument("--root", default="", help="资料目录，用于找同分类号目录里的文件")
    parser.add_argument("--sheet", default="复核表")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="评测/llm复判结果.csv")
    args = parser.parse_args()

    config = LLMConfig.from_env()
    if not config.ready:
        print(f"缺少配置：{config.missing}")
        return 2
    print(f"模型：{config.style} / {config.model} @ {config.base_url}")

    workbook = openpyxl.load_workbook(BASE / args.gold, data_only=True)
    sheet = workbook[args.sheet]

    records = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        seq, code, text, tool_state, files_cell, human, why = row[:7]
        if seq is None or not text:
            continue
        records.append({
            "seq": str(seq).strip(),
            "code": str(code or "").strip(),
            "text": str(text).strip(),
            "tool_state": str(tool_state or "").strip(),
            "files": str(files_cell or ""),
            "human": str(human or "").strip(),
            "why": str(why or "").strip(),
        })
    if args.limit:
        records = records[: args.limit]

    root = Path(args.root) if args.root else infer_root(records)
    all_files = []
    if root and root.exists():
        print(f"扫描资料目录找同分类号文件：{root}")
        all_files = scan_directory(root)
        print(f"  共 {len(all_files)} 个文件")
    else:
        print("没有可用的资料目录，退化为只用单元格里给出的候选文件")

    real_checkpoints = load_real_checkpoints()
    reader = ContentReader(use_ocr=True, max_pages=2, check_seal=False,
                           cache_path=BASE / "data" / "llm_eval_cache.json")
    judge = LLMJudge(config, cache_path=BASE / "data" / "llm_cache.json")
    case_files, _ = case_index(all_files)
    cases, truncated = case_list(all_files)

    results = []
    for index, record in enumerate(records, start=1):
        candidates = expand_candidates(split_paths(record["files"]))
        excerpt = read_excerpt(candidates, reader) if candidates else ""
        nearby = nearby_names(record["code"], all_files, limit=40) if all_files else []
        if not nearby and candidates:
            nearby = [p.name for p in candidates][:40]

        checkpoint = pick_checkpoint(real_checkpoints, record["code"], record["text"])
        if checkpoint is None:
            checkpoint = Checkpoint(
                id=record["seq"], node_key="", code=record["code"], category="",
                path=[], archive_content="", text=record["text"], seq=0,
                source="归档范围表（未匹配到真表条目）", filing="", retention={},
            )
        key = f"{checkpoint.text}{checkpoint.archive_content}"
        related = related_names(key, all_files, limit=12)
        folders = related_folders(key, all_files)
        result = MatchResult(checkpoint=checkpoint, state="缺失", score=0.0)
        # 候选文件名并进 nearby：既让模型看到「这一条实际交付了什么」，
        # 也进 quote 校验范围——凭文件名给的「已提供」同样要能对上原文
        request = JudgeRequest(
            result=result,
            excerpt=excerpt,
            nearby=tuple(dict.fromkeys([p.name for p in candidates] + nearby)),
            related=tuple(related),
            cases=tuple(cases),
            cases_truncated=truncated,
            case_map=case_files,
            fallback_folders=tuple(folders),
        )
        verdict = judge_request(judge, request)

        expected = EXPECT.get(record["human"], "")
        match = "" if not expected else ("命中" if verdict.verdict == expected else "不一致")
        results.append({**record, "llm": verdict.verdict, "label": verdict.label,
                        "reason": verdict.reason, "quote": verdict.quote,
                        "error": verdict.error, "expected": expected,
                        "judged": match, "excerpt_chars": len(excerpt),
                        "nearby": len(nearby)})
        print(f"[{index}/{len(records)}] {record['code']} {record['text'][:26]} "
              f"人工={record['human'] or '—'} LLM={VERDICT_LABELS.get(verdict.verdict, '?')} "
              f"{match}{('  ' + verdict.error) if verdict.error else ''}")

    judge.save()
    reader.save()

    scored = [item for item in results if item["expected"]]
    hit = sum(1 for item in scored if item["judged"] == "命中")
    print("\n--- 混淆 ---")
    for human in ("真缺", "口径差异"):
        subset = [item for item in results if item["human"] == human]
        if not subset:
            continue
        for verdict in ("match", "gap", "unclear"):
            count = sum(1 for item in subset if item["llm"] == verdict)
            if count:
                print(f"  人工={human} → LLM={VERDICT_LABELS[verdict]}: {count}")
    if scored:
        print(f"\n计分 {len(scored)} 条，一致 {hit} 条（{hit / len(scored):.0%}）")
        print(f"其中：真缺 {sum(1 for i in scored if i['human'] == '真缺')} 条 / "
              f"口径差异 {sum(1 for i in scored if i['human'] == '口径差异')} 条")
    print(f"实调 {judge.calls} 次、缓存命中 {judge.hits} 次、失败 {judge.errors} 次")

    out_path = BASE / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["seq", "code", "text", "human", "expected", "llm", "label",
                        "reason", "quote", "judged", "error", "why", "files"],
        )
        writer.writeheader()
        for item in results:
            writer.writerow({key: item.get(key, "") for key in writer.fieldnames})
    print(f"明细：{out_path}（含真实路径，不要提交）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
