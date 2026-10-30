"""统一模版入口：docx / doc / yaml。"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .docx_parser import TemplateParseError, parse_docx
from .yaml_loader import parse_yaml


def load_template(path: str | Path):
    path = Path(path)
    if not path.exists():
        raise TemplateParseError(f"模版文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return parse_docx(path)
    if suffix in (".yaml", ".yml"):
        return parse_yaml(path)
    if suffix == ".doc":
        converted = convert_doc_to_docx(path)
        return parse_docx(converted)
    raise TemplateParseError(f"不支持的模版格式：{suffix}，请上传 .docx / .doc / .yaml")


def _convert_with_word(src: Path, out: Path) -> bool:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception:
        return False

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(
            str(src.resolve()), ReadOnly=True, AddToRecentFiles=False, Visible=False
        )
        doc.SaveAs2(str(out.resolve()), FileFormat=16)  # wdFormatDocumentDefault
        return out.exists()
    except Exception:
        return False
    finally:
        try:
            if doc is not None:
                doc.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _convert_with_libreoffice(src: Path, out_dir: Path) -> Path | None:
    soffice = _find_soffice()
    if not soffice:
        return None
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(src)],
            check=True,
            timeout=180,
            capture_output=True,
        )
    except Exception:
        return None
    expected = out_dir / (src.stem + ".docx")
    return expected if expected.exists() else None


def convert_doc_to_docx(path: str | Path) -> Path:
    src = Path(path)
    out_dir = Path(tempfile.mkdtemp(prefix="gsdoc_"))
    out = out_dir / (src.stem + ".docx")

    if _convert_with_word(src, out):
        return out

    converted = _convert_with_libreoffice(src, out_dir)
    if converted:
        return converted

    raise TemplateParseError(
        "旧版 .doc 需要本机安装 Microsoft Word 或 LibreOffice 才能转换。"
        "建议先用 Word 另存为 .docx 再上传，或改用 YAML 模版。"
    )
