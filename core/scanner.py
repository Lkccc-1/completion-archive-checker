"""目录扫描：只读、跳过临时文件，记录相对路径和分类编码。"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

from .models import FileRecord
from .text import extract_codes

ProgressFn = Callable[[int, int], None]

DEFAULT_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".jpg", ".jpeg", ".png",
    ".tif", ".tiff", ".bmp", ".dwg", ".zip", ".rar", ".7z",
}

IGNORE_DIRS = {
    ".git", ".svn", ".idea", ".vscode", "__MACOSX", "node_modules",
    "System Volume Information", "$RECYCLE.BIN",
}


def _stat(full: Path) -> os.stat_result | None:
    try:
        return full.stat()
    except OSError:
        if os.name != "nt":
            return None
    # Windows 长路径兜底
    try:
        return Path("\\\\?\\" + str(full.resolve())).stat()
    except OSError:
        return None


def scan_directory(
    root: str | Path,
    extensions: set[str] | None = None,
    progress: ProgressFn | None = None,
) -> list[FileRecord]:
    root = Path(str(root).strip().strip('"').strip("'")).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"目录不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录：{root}")

    allowed = {ext.lower() if ext.startswith(".") else "." + ext.lower() for ext in (extensions or DEFAULT_EXTENSIONS)}

    paths: list[tuple[Path, os.stat_result]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and name not in IGNORE_DIRS and not name.startswith("~$")
        ]
        for filename in filenames:
            if filename.startswith("~$") or filename.startswith("."):
                continue
            suffix = Path(filename).suffix.lower()
            if suffix not in allowed:
                continue
            full = Path(dirpath) / filename
            info = _stat(full)
            if info is None or not stat.S_ISREG(info.st_mode):
                continue
            paths.append((full, info))

    total = len(paths)
    files: list[FileRecord] = []
    for index, (full, info) in enumerate(paths, start=1):
        try:
            rel_path = full.relative_to(root).as_posix()
        except ValueError:
            rel_path = full.as_posix()
        parts = rel_path.split("/")[:-1]
        files.append(
            FileRecord(
                rel_path=rel_path,
                dir_parts=parts,
                name=full.name,
                stem=full.stem,
                ext=full.suffix.lower(),
                size=info.st_size,
                mtime=info.st_mtime,
                codes=extract_codes(rel_path),
            )
        )
        if progress and (index % 25 == 0 or index == total):
            progress(index, total)

    files.sort(key=lambda item: item.rel_path)
    return files
