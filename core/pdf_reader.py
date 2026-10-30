"""PDF / 扫描件正文读取：优先文字层，没有文字层再走本地 OCR。

两个省时间的关键点：
1. **先文字层**。电子版 PDF（卷内目录、报表导出版）直接 ``get_text()``，毫秒级，
   只有真扫描件才渲染成图跑 OCR。
2. **读到够用就停**。调用方传 ``stop_when(text_so_far, page_no)``，一旦已经能确认
   这份文件对应的检查点，就不再往下读——多读的每一页都是纯成本。

OCR 用 rapidocr-onnxruntime（本地、离线、中文），200dpi 一页约 0.8–2 秒。
结果按「文件 + 修改时间」缓存在项目目录下，重复检查不重新识别。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .seal import SealScan, detect_seals, color_ratio, scan_seals

CACHE_VERSION = 5
OCR_DPI = 200
SEAL_DPI = 120
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXTENSIONS = {".pdf"} | IMAGE_EXTENSIONS

StopFn = Callable[[str, int], bool]
ProgressFn = Callable[[int, int, str], None]

_OCR_ENGINE = None


def ocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


@dataclass
class PageContent:
    number: int
    text: str
    source: str = "text"          # text / ocr / empty


@dataclass
class DocumentContent:
    rel_path: str = ""
    total_pages: int = 0
    pages: list[PageContent] = field(default_factory=list)
    stopped_early: bool = False
    ocr_pages: int = 0
    seal_state: str = "未知"
    seal_pages: list[int] = field(default_factory=list)
    seal_count: int = 0
    color_ratio: float = 0.0
    error: str = ""
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages if page.text)

    @property
    def chars(self) -> int:
        return len(self.text.replace("\n", ""))

    @property
    def read_pages(self) -> int:
        return len(self.pages)

    @property
    def used_ocr(self) -> bool:
        return self.ocr_pages > 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["elapsed"] = round(self.elapsed, 2)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentContent":
        pages = [PageContent(**item) for item in data.get("pages", [])]
        return cls(
            rel_path=data.get("rel_path", ""),
            total_pages=data.get("total_pages", 0),
            pages=pages,
            stopped_early=data.get("stopped_early", False),
            ocr_pages=data.get("ocr_pages", 0),
            seal_state=data.get("seal_state", "未知"),
            seal_pages=list(data.get("seal_pages", [])),
            seal_count=data.get("seal_count", 0),
            color_ratio=data.get("color_ratio", 0.0),
            error=data.get("error", ""),
            elapsed=0.0,
        )


def open_document(path: Path):
    """打开 PDF，超长 Windows 路径走 \\\\?\\ 前缀重试。"""
    import pymupdf

    try:
        return pymupdf.open(path)
    except Exception:
        resolved = Path("\\\\?\\" + str(Path(path).resolve()))
        return pymupdf.open(resolved)


def _pixmap_to_array(pixmap) -> np.ndarray:
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    if pixmap.n == 4:
        image = image[:, :, :3]
    elif pixmap.n == 1:
        image = np.repeat(image, 3, axis=2)
    return np.ascontiguousarray(image)


def _ocr_text(image: np.ndarray) -> str:
    height, width = image.shape[:2]
    # 超过 2500px 宽的图先缩一档，OCR 速度和精度都更好
    if width > 2500:
        scale = 2500 / width
        image = _resize(image, scale)
    result, _ = _ocr_engine()(image)
    lines = [item[1] for item in (result or []) if len(item) > 1 and item[1]]
    return "\n".join(lines)


def _resize(image: np.ndarray, scale: float) -> np.ndarray:
    import cv2

    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


class ContentReader:
    """读取 PDF/图片正文与印章，带缓存和提前停止。"""

    def __init__(
        self,
        *,
        use_ocr: bool = True,
        max_pages: int = 3,
        check_seal: bool = True,
        cache_path: str | Path | None = None,
    ) -> None:
        self.use_ocr = use_ocr and ocr_available()
        self.max_pages = max(1, int(max_pages))
        self.check_seal = check_seal
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        self.ocr_pages = 0
        self.seconds = 0.0
        if self.cache_path and self.cache_path.exists():
            try:
                raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if raw.get("version") == CACHE_VERSION:
                    self._cache = raw.get("items", {})
            except Exception:  # noqa: BLE001
                self._cache = {}

    # ------------------------------------------------------------------
    def _key(self, path: Path) -> str:
        try:
            info = path.stat()
            stamp = f"{int(info.st_mtime)}-{info.st_size}"
        except OSError:
            stamp = "0-0"
        raw = f"{path.as_posix()}|{stamp}|{self.max_pages}|{int(self.use_ocr)}|{int(self.check_seal)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def save(self) -> None:
        if not self.cache_path or not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "items": self._cache}
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        self._dirty = False

    # ------------------------------------------------------------------
    def read(
        self,
        abs_path: str | Path,
        rel_path: str = "",
        stop_when: StopFn | None = None,
    ) -> DocumentContent:
        path = Path(abs_path)
        rel_path = rel_path or path.name
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return DocumentContent(rel_path=rel_path, error="不支持的文件类型")

        key = self._key(path)
        cached = self._cache.get(key)
        base: DocumentContent | None = None
        if cached is not None:
            base = DocumentContent.from_dict(cached)
            if stop_when is None or not base.stopped_early:
                # 读全了的直接复用
                self.hits += 1
                return base
            if stop_when(base.text, base.read_pages):
                # 上次是因为“读到够用”才停的，这次判定条件仍成立，同样可以复用
                self.hits += 1
                return base
            # 判定条件变了，需要接着往下读：复用已读页，只补后面的

        self.misses += 1
        started = time.perf_counter()
        try:
            if path.suffix.lower() == ".pdf":
                doc = self._read_pdf(path, rel_path, stop_when, base=base)
            else:
                doc = self._read_image(path, rel_path)
        except Exception as exc:  # noqa: BLE001
            doc = DocumentContent(rel_path=rel_path, error=f"{type(exc).__name__}: {exc}")
        doc.elapsed = time.perf_counter() - started
        self.seconds += doc.elapsed
        # 接着上次读的文档，只统计本次新 OCR 的页
        self.ocr_pages += doc.ocr_pages - (base.ocr_pages if base else 0)

        # 提前停止的也缓存（标 stopped_early），下次判定条件仍成立时可直接复用
        if not doc.error:
            self._cache[key] = doc.to_dict()
            self._dirty = True
            if len(self._cache) > 4000:
                self._cache = dict(list(self._cache.items())[-3000:])
        return doc

    # ------------------------------------------------------------------
    def _read_pdf(
        self,
        path: Path,
        rel_path: str,
        stop_when: StopFn | None,
        base: DocumentContent | None = None,
    ) -> DocumentContent:
        out = DocumentContent(rel_path=rel_path)
        if base is not None:
            out.pages = list(base.pages)
            out.seal_pages = list(base.seal_pages)
            out.seal_count = base.seal_count
            out.color_ratio = base.color_ratio
            out.ocr_pages = base.ocr_pages
            out.stopped_early = False
        start = len(out.pages)
        with open_document(path) as document:
            out.total_pages = document.page_count
            limit = min(document.page_count, self.max_pages)
            for index in range(start, limit):
                page = document[index]
                text = (page.get_text("text") or "").strip()
                image = None
                if not text and self.use_ocr:
                    image = _pixmap_to_array(page.get_pixmap(dpi=OCR_DPI))
                    text = _ocr_text(image)
                    out.ocr_pages += 1
                    out.pages.append(PageContent(number=index + 1, text=text, source="ocr"))
                else:
                    out.pages.append(
                        PageContent(number=index + 1, text=text,
                                    source="text" if text else "empty")
                    )
                if self.check_seal:
                    if image is None:
                        image = _pixmap_to_array(page.get_pixmap(dpi=SEAL_DPI))
                        seals = detect_seals(image, page=index + 1, dpi=SEAL_DPI)
                        ratio = color_ratio(image)
                    else:
                        seals = detect_seals(image, page=index + 1, dpi=OCR_DPI)
                        ratio = color_ratio(image)
                    if seals:
                        out.seal_pages.append(index + 1)
                        out.seal_count += len(seals)
                    out.color_ratio = max(out.color_ratio, ratio)

                if stop_when and index + 1 < limit and stop_when(out.text, index + 1):
                    out.stopped_early = True
                    break
        self._finalize_seal(out)
        return out

    def _read_image(self, path: Path, rel_path: str) -> DocumentContent:
        import cv2

        out = DocumentContent(rel_path=rel_path, total_pages=1)
        # cv2.imread 不支持中文路径，先读字节再解码
        buffer = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR) if buffer.size else None
        if image is None:
            raise ValueError("图片读取失败")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        text = _ocr_text(image) if self.use_ocr else ""
        out.pages.append(PageContent(number=1, text=text, source="ocr" if text else "empty"))
        if text:
            out.ocr_pages += 1
        if self.check_seal:
            seals = detect_seals(image, page=1, dpi=OCR_DPI)
            if seals:
                out.seal_pages.append(1)
                out.seal_count += len(seals)
            out.color_ratio = color_ratio(image)
        self._finalize_seal(out)
        return out

    @staticmethod
    def _finalize_seal(out: DocumentContent) -> None:
        scan = SealScan(
            state="有" if out.seal_count else "",
            pages=list(out.seal_pages),
            count=out.seal_count,
            color_ratio=out.color_ratio,
        )
        if out.seal_count:
            scan.state = "有"
        elif out.color_ratio < 0.004:
            scan.state = "未知"
            scan.note = "黑白扫描件，红色印章无法判定"
        else:
            scan.state = "无"
            scan.note = "彩色页面上未检出红色印章"
        out.seal_state = scan.state
