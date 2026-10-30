"""按正文内容给文件起名：读取 → 提标题 → 生成短文件名 → 落盘改名。

规则和检查器里读正文的做法一致：先文字层、没有再 OCR，读到能定名就停。
命名思路是「能用文件自带的标题就不用自己总结」——红头文件的标题、表格的表名
本身就是最准的文件名；只有标题里夹着工程全名、单位名、文号时才做删减，
最后控制在 40 字以内，优先短。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

MAX_NAME = 40
SUPPORTED = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# 文种词：出现这些词基本就能确定这份文件是什么。长的放前面，避免“记录”抢先。
DOC_WORDS = (
    "分项工程质量验收记录", "分部工程质量验收记录", "隐蔽工程验收记录",
    "施工质量验收及评定项目划分报审表", "质量验收及评定项目划分报审表",
    "安全技术及文明施工交底单", "安全技术交底记录表", "安全技术交底",
    "作业指导书", "施工工艺导则",
    "施工组织设计", "专项施工方案", "施工方案", "施工进度计划", "三级进度计划",
    "工程开工报审表", "开工报告", "竣工验收报告", "竣工报告", "竣工图",
    "质量验收记录", "检查及评级记录", "评级记录", "评定表", "验收记录", "验收表",
    "报审表", "报验表", "申请表", "审批表", "备案表", "划分表", "汇总表", "明细表",
    "检测报告", "试验报告", "检验报告", "开工报告", "报告书", "报告",
    "责任书", "承诺书", "授权书", "委托书", "任命书",
    "交底记录", "记录表", "记录", "台账", "日志", "清单", "证明书", "证明",
    "合格证", "证书", "批复", "意见书", "意见", "函", "通知", "纪要",
    "计划", "横道图", "签证", "图纸", "说明", "总结", "协议", "合同",
)

_NUMBER_ONLY = re.compile(r"^[\d\s.．、\-—_/()（）]+$")
_DATE_LINE = re.compile(r"^[\d〇零一二三四五六七八九十]{2,4}[年.\-/]\s*\d{1,2}\s*[月.\-/]?\s*\d{0,2}\s*日?\s*$")
_UNIT_LINE = re.compile(
    r"^[一-龥（）()·]{2,24}?(有限公司|有限责任公司|公司|供电局|电力局|设计院|"
    r"监理项目部|项目部|分公司)(文件)?$"
)
_DOC_NO = re.compile(r"^[一-龥]{2,10}\s*[〔\[［(（]?\s*\d{4}\s*[〕\]］)）]\s*\d*\s*号?$")
_FIELD_LINE = re.compile(
    r"^(工程名称|项目名称|分项工程名称|工程编号|建设单位|施工单位|设计单位|监理单位|"
    r"检验批|桩号|部位|致|抄送|附件|编号|编码|"
    r"合同编号|文件号|表号|日期|日\s*期)\s*[:：]"
)
_NUMBERED = re.compile(r"^(?:[一二三四五六七八九十]{1,3}|\d{1,2})\s*[、.．)）]|^[（(]\d{1,2}[)）]")
_TABLE_NO = re.compile(r"^\s*(Q/CSG\s*)?表\s*[A-Za-z]?[\d.\-—]*\s*[*＊★]?\s*(?=\S)")
# 卷内档号/分类号标签：【S005】这种，跟文件名无关
_BRACKET_CODE = re.compile(r"^\s*(【[^】]{1,12}】|\[[^\]]{1,12}\])\s*")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
_CJK = re.compile(r"[一-龥]")

# 成套发布的技术文件封面都有“系列抬头”：发文单位 + 这套书的名字（第X部分：…）。
# 抬头是整套书的书名，不是这份文件的题目，文件名只要冒号后面那段主体的题目；
# 抬头里带的文种（作业指导书）补到题目末尾，否则名字里就丢了“这是什么文件”。
_SERIES_WORDS = (
    "作业指导书", "技术标准", "企业标准", "技术规范", "验收规范", "施工工艺导则",
    "实施细则", "实施导则", "规范", "标准", "手册", "图集", "导则", "细则",
)
# 抬头里能直接当文种补到题目后面的（“规范/手册”在抬头里多半只是书名，补上去反而怪）
_SERIES_DOC_WORDS = ("作业指导书", "技术标准", "企业标准")
_PART_MARK = re.compile(r"第\s*[0-9一二三四五六七八九十]{1,3}\s*(?:部分|分册|篇|册|章)\s*[:：]?\s*")
_LEADING_PART = re.compile(r"^第\s*[0-9一二三四五六七八九十]{1,3}\s*(?:部分|分册|篇|册|章)\s*[:：]?\s*")
_SERIES_COLON = re.compile(
    r"(?:作业指导书|技术标准|企业标准|技术规范|实施细则|导则|手册|图集|规范|标准)\s*[:：]"
)
# 抬头上的发文单位：出现这些词才认为冒号前面是“单位 + 书名”，不是正文里的普通冒号
_ORG_WORDS = ("有限责任公司", "有限公司", "公司", "电网", "供电局", "电力局", "集团")
_ORG_ONLY = re.compile(r"^[一-龥]{2,8}(电网|电力|集团)$")
_LATIN_ONLY = re.compile(r"^[A-Za-z0-9.&/()·\-]+$")
# 图纸的图名在图签里：“平断面图3（1A11A~2A1）”这种带括号范围的短行
_DRAWING_TITLE = re.compile(r"^[一-龥]{2,10}图\s*[\d一二三四五六七八九十]*\s*[（(][^）)\n]{1,24}[）)]$")


def _is_series_head(line: str) -> bool:
    """“中国南方电网有限责任公司电网建设施工作业指导书”这种整套书的抬头。

    抬头对同一套书里每一份都一样，认不出是哪一份；但它后面直接跟着题目
    （冒号后面还有字）时不能丢，那种要留着一起拆。
    """
    if not any(word in line for word in _SERIES_WORDS):
        return False
    if not any(word in line for word in _ORG_WORDS):
        return False
    return not re.search(r"[：:]\s*\S", line)


@dataclass
class RenameItem:
    """一份文件的改名方案。"""

    path: Path
    old_name: str
    new_name: str = ""
    ext: str = ""
    source: str = ""
    title: str = ""
    note: str = ""
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.new_name) and self.new_name + self.ext != self.old_name


def _flat(text: str) -> str:
    """中文标题里的空格/换行都是排版产物，去掉再比对。"""
    return re.sub(r"[ \t　]+", "", text)


def _has_doc_word(text: str) -> bool:
    return any(word in text for word in DOC_WORDS)


def _ends_with_doc_word(text: str) -> bool:
    return any(text.endswith(word) for word in DOC_WORDS)


def _is_noise(line: str) -> bool:
    if not line:
        return True
    line = _BRACKET_CODE.sub("", line).strip()
    if not line:
        return True
    if _NUMBER_ONLY.match(line):
        return True
    if _DATE_LINE.match(line):
        return True
    if _UNIT_LINE.match(line):
        return True
    if _DOC_NO.match(line):
        return True
    if _FIELD_LINE.match(line):
        return True
    if re.fullmatch(r"(Q/CSG\s*)?表\s*[A-Za-z]?\s*[\d.\-—]*\s*[*＊★]?", line):
        return True
    if _is_series_head(line) or _ORG_ONLY.match(line) or _LATIN_ONLY.match(line):
        return True
    return False


def _strip_prefix(text: str) -> str:
    text = _BRACKET_CODE.sub("", text)
    text = _TABLE_NO.sub("", text)
    text = text.strip(" *_＊★　")
    return text


def _strip_series_head(name: str) -> str:
    """去掉封面上的系列抬头，只留这份文件的题目。

    “中国南方电网…电网建设施工作业指导书第1部分：输电线路外拉线悬浮抱杆分解组立铁塔”
    → “输电线路外拉线悬浮抱杆分解组立铁塔作业指导书”。
    抬头（发文单位 + 整套书的书名 + 第几部分）对每一份都一样，认不出是哪份文件；
    冒号后面才是这份文件的题目。抬头里的“作业指导书”是文种，补到题目末尾。
    """
    name = _LEADING_PART.sub("", name)
    for mark in (_PART_MARK, _SERIES_COLON):
        match = mark.search(name)
        if not match:
            continue
        head, tail = name[: match.start()], name[match.end():].strip(":： ")
        if len(tail) < 4 or not _CJK.search(tail):
            continue
        context = head + match.group(0)
        if not any(word in context for word in _SERIES_WORDS + _ORG_WORDS):
            continue
        doc_word = next((word for word in _SERIES_DOC_WORDS if word in context), "")
        if doc_word and doc_word not in tail:
            tail += doc_word
        return tail
    return name


def find_project_names(text: str, extra: Iterable[str] = ()) -> list[str]:
    """找出工程全名，用于从标题里删掉（工程名不是文件主体）。

    两条路：`工程名称：…` 这种显式字段最可靠；没有字段时就找正文里
    重复出现的“…工程”（同一份文件里出现两次以上才认，避免把“开工报告”
    这类词当成工程名）。
    """
    flat = re.sub(r"\s+", "", text)
    names: set[str] = {item.strip("-—、 ") for item in extra if item and len(item.strip()) >= 4}
    for match in re.finditer(r"(?:工程名称|项目名称)[:：]\s*([^\n]{4,80})", text):
        raw = match.group(1)
        raw = re.split(r"(?:建设单位|施工单位|监理单位|设计单位|编号|合同)", raw)[0]
        for part in re.split(r"[-—－]", raw):
            part = _flat(part).strip("，。；、 ")
            if 6 <= len(part) <= 60 and "工程" in part:
                names.add(part)

    # 频次法：从“……工程”往前取窗口，再取最短的、在正文里出现 ≥2 次的尾串。
    # 红头文件的标题和正文会各写一遍工程全名，重复两次才认，避免把常用短语当工程名。
    for match in re.finditer("工程", flat):
        chunk = flat[max(0, match.start() - 30): match.end()]
        for cut in range(0, len(chunk) - 5):
            tail = chunk[cut:]
            if flat.count(tail) >= 2:
                names.add(_strip_leading_verb(tail, flat))
                break
    return sorted(names, key=len, reverse=True)


_LEADING_VERBS = ("成立", "推进", "完成", "承担", "建设", "实施", "负责", "组织", "承建", "建设")


def _strip_leading_verb(text: str, flat: str) -> str:
    """“成立某市…工程”里的“成立”是动词，不是工程名的一部分。"""
    changed = True
    while changed:
        changed = False
        for verb in _LEADING_VERBS:
            if text.startswith(verb) and len(text) - len(verb) >= 6 \
                    and flat.count(text[len(verb):]) >= 2:
                text = text[len(verb):]
                changed = True
    return text


def _merge_lines(lines: list[str], start: int) -> str:
    """标题常被排版拆成两三行，往后接，直到出现文种词。"""
    merged = _strip_prefix(lines[start])
    index = start + 1
    merged_lines = 0
    while index < len(lines) and len(merged) < 80 and merged_lines < 3:
        nxt = _strip_prefix(lines[index])
        if not nxt or _is_noise(nxt) or nxt.endswith(("：", ":")):
            break
        if _ends_with_doc_word(merged) or _NUMBERED.match(nxt):
            break
        merged_lines += 1
        # 单独一个“图/函/件”这种 OCR 残字不接
        if len(nxt) <= 2 and not _has_doc_word(nxt):
            index += 1
            continue
        merged += nxt
        index += 1
    return merged


def drawing_name(text: str) -> str:
    """图纸的题目在图签（右下角），正文开头是图上的注记，当不了文件名。

    只在认出是图纸（有出图专用章）时才用，避免把普通文件里带括号的行当图名。
    """
    if "出图专用章" not in _flat(text):
        return ""
    for line in text.splitlines():
        line = _flat(line).strip("，。；、 ")
        if _DRAWING_TITLE.match(line):
            return line
    return ""


def extract_title(text: str) -> str:
    """从正文里取标题：优先带文种词的行，其次第一段像标题的内容。"""
    raw_lines = [line.strip() for line in text.splitlines()]
    lines = [_flat(line) for line in raw_lines if line.strip() and not _is_noise(_flat(line))]
    fallback = ""
    for index, line in enumerate(lines[:12]):
        if len(_strip_prefix(line)) < 4:
            continue
        merged = _merge_lines(lines, index)
        if _has_doc_word(merged):
            return merged
        if not fallback and 6 <= len(merged) <= 60:
            fallback = merged
    return fallback


def summarize_name(text: str, *, project_name: str = "") -> str:
    """把正文归纳成文件名（不含扩展名，≤40 字）。"""
    title = extract_title(text)
    if not title:
        return ""
    name = _flat(title)
    if not _has_doc_word(name):
        name = drawing_name(text) or name
    name = _strip_series_head(name)
    name = _strip_prefix(name).strip("，。；、:：-—_ 　")

    for project in find_project_names(text, [project_name]):
        if project and project in name:
            name = name.replace(project, "")

    name = re.sub(r"^关于", "", name)
    name = re.sub(r"的+$", "", name)
    name = name.strip("，。；、:：-—_ 　").replace("_", "")
    name = re.sub(r"(?<=[一-龥])/+(?=[一-龥])", "", name)
    name = name.replace("（", "(").replace("）", ")")
    name = _ILLEGAL.sub("-", name).strip(" .")

    if len(name) > MAX_NAME:
        cut = max(
            (name.rfind(sep, 0, MAX_NAME) for sep in "、，,;；"),
            default=-1,
        )
        name = name[:cut] if cut >= 12 else name[:MAX_NAME]
        name = name.strip("、，,;； ")
    return name


def sanitize(name: str) -> str:
    name = _ILLEGAL.sub("-", name).strip(" .")
    if name.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    return name[:MAX_NAME].strip(" .")


# ----------------------------------------------------------------------
# 读正文
# ----------------------------------------------------------------------
def _read_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs[:80]]
    for table in doc.tables[:2]:
        for row in table.rows[:12]:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(part for part in parts if part.strip())


def read_text(
    path: Path,
    *,
    max_pages: int = 2,
    use_ocr: bool = True,
    reader=None,
) -> tuple[str, str, str]:
    """返回 (正文, 来源, 错误)。PDF/图片走 ContentReader，Word 直接解析。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        try:
            return _read_docx(path), "Word", ""
        except Exception as exc:  # noqa: BLE001
            return "", "", f"{type(exc).__name__}: {exc}"
    if suffix == ".doc":
        try:
            from .template_loader import convert_doc_to_docx

            converted = convert_doc_to_docx(path)
            return _read_docx(converted), "Word", ""
        except Exception as exc:  # noqa: BLE001
            return "", "", f"{type(exc).__name__}: {exc}"

    from .pdf_reader import ContentReader

    reader = reader or ContentReader(use_ocr=use_ocr, max_pages=max_pages, check_seal=False)

    def enough(text: str, _pages: int) -> bool:
        return bool(extract_title(text))

    doc = reader.read(path, stop_when=enough)
    if doc.error:
        return doc.text, "", doc.error
    source = "OCR" if doc.ocr_pages else "文字层"
    return doc.text, source, ""


# ----------------------------------------------------------------------
# 批量生成方案 / 执行 / 撤销
# ----------------------------------------------------------------------
def iter_files(root: Path) -> list[Path]:
    root = Path(root)
    return sorted(
        (item for item in root.rglob("*")
         if item.is_file() and item.suffix.lower() in SUPPORTED and not item.name.startswith(("~$", "."))),
        key=lambda item: item.as_posix().lower(),
    )


def plan_folder(
    root: str | Path,
    *,
    project_name: str = "",
    max_pages: int = 2,
    use_ocr: bool = True,
    progress: Callable[[int, int], None] | None = None,
    cache_path: str | Path | None = None,
) -> list[RenameItem]:
    root = Path(root)
    files = iter_files(root)
    items: list[RenameItem] = []
    reader = None
    if any(f.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"} for f in files):
        from .pdf_reader import ContentReader

        reader = ContentReader(
            use_ocr=use_ocr, max_pages=max_pages, check_seal=False, cache_path=cache_path
        )
    used: dict[str, int] = {}
    for index, path in enumerate(files, start=1):
        text, source, error = read_text(
            path, max_pages=max_pages, use_ocr=use_ocr, reader=reader
        )
        item = RenameItem(
            path=path,
            old_name=path.name,
            ext=path.suffix,
            source=source,
            error=error,
            title=extract_title(text),
        )
        name = summarize_name(text, project_name=project_name) if text else ""
        if not name:
            item.note = "没能从正文认出标题，保留原名" if not error else "读不了这份文件，保留原名"
            name = path.stem
        else:
            item.new_name = sanitize(name)
        if item.new_name:
            key = (item.new_name + item.ext).lower()
            used[key] = used.get(key, 0) + 1
            if used[key] > 1:
                item.new_name = sanitize(f"{item.new_name}_{used[key]}")
            while (path.parent / (item.new_name + item.ext)).exists() and \
                    (path.parent / (item.new_name + item.ext)) != path:
                used[key] += 1
                item.new_name = sanitize(f"{sanitize(name)}_{used[key]}")
        items.append(item)
        if progress:
            progress(index, len(files))
    if reader is not None:
        reader.save()
    return items


def log_path(root: str | Path, *, base: Path | None = None) -> Path:
    """改名日志：按文件夹路径取哈希，一个文件夹一份，改错了能按批次倒着改回来。"""
    key = hashlib.sha1(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()[:12]
    data_dir = Path(base) if base else Path("data")
    return data_dir / "rename_logs" / f"{key}.json"


def apply_plan(items: list[RenameItem], *, log_file: Path | None = None) -> dict:
    """真正改文件名。每次操作记一条日志，撤销时按日志倒序改回来。"""
    done: list[dict] = []
    failed: list[dict] = []
    for item in items:
        if not item.changed:
            continue
        src = item.path
        dst = src.with_name(item.new_name + item.ext)
        if dst.exists():
            failed.append({"old": src.name, "new": dst.name, "reason": "目标已存在"})
            continue
        try:
            src.rename(_long(dst))
            done.append({
                "old": str(src),
                "new": str(dst),
                "old_name": src.name,
                "new_name": dst.name,
            })
            item.path = dst
            item.old_name = dst.name
        except OSError as exc:
            failed.append({"old": src.name, "new": dst.name, "reason": str(exc)})
    if log_file and done:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        if log_file.exists():
            try:
                payload = json.loads(log_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                payload = []
        payload.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "items": done,
        })
        log_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"done": done, "failed": failed}


def undo_last(log_file: Path) -> dict:
    if not log_file.exists():
        return {"done": [], "failed": [], "reason": "没有改名记录"}
    batches = json.loads(log_file.read_text(encoding="utf-8"))
    if not batches:
        return {"done": [], "failed": [], "reason": "没有改名记录"}
    batch = batches.pop()
    done: list[dict] = []
    failed: list[dict] = []
    for item in reversed(batch["items"]):
        src = Path(item["new"])
        dst = Path(item["old"])
        if not src.exists():
            failed.append({"old": item["new_name"], "new": item["old_name"], "reason": "文件已不在"})
            continue
        if dst.exists():
            failed.append({"old": item["new_name"], "new": item["old_name"], "reason": "原名已被占用"})
            continue
        try:
            src.rename(_long(dst))
            done.append(item)
        except OSError as exc:
            failed.append({"old": item["new_name"], "new": item["old_name"], "reason": str(exc)})
    log_file.write_text(json.dumps(batches, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"done": done, "failed": failed, "time": batch.get("time", "")}


def _long(path: Path) -> str | Path:
    if len(str(path)) < 240:
        return path
    return "\\\\?\\" + str(path.resolve())
