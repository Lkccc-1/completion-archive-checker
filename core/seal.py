"""红色印章识别（启发式，不依赖模型）。

思路：扫描件的印章都是红色油墨，在 HSV 里与黑白正文分离得很干净。
先用红色掩膜取出候选像素，再做一次膨胀把“圆环 + 环内文字”粘成一块，
最后按尺寸、长宽比和红色密度筛掉表格线、红头文件横条、彩色照片。

判定分三档，避免把黑白扫描件误判成“没盖章”：
- ``有``：检出符合印章形态的红色块；
- ``无``：页面是彩色的，但没有红色印章；
- ``未知``：整页几乎无彩色像素（黑白扫描/复印），红章检测不成立。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

# 一整页彩色像素占比低于该值，视为黑白扫描
COLOR_PAGE_MIN = 0.004
# 印章外接框占页宽的最大比例，再大就是红头/横幅/照片
MAX_BOX_RATIO = 0.45
# 外接框内红色像素的最低密度
MIN_RED_DENSITY = 0.05


@dataclass
class SealBox:
    page: int
    x: int
    y: int
    w: int
    h: int
    area: int

    @property
    def size_mm(self) -> float:
        """按 A4 210mm 宽、常见 4cm 圆章换算的直观尺寸说明。"""
        return round(self.w / 10, 1)


@dataclass
class SealScan:
    state: str = "未知"          # 有 / 无 / 未知
    pages: list[int] = field(default_factory=list)
    count: int = 0
    color_ratio: float = 0.0
    note: str = ""

    @property
    def text(self) -> str:
        if self.state == "有":
            where = "、".join(f"第{p}页" for p in self.pages[:4])
            extra = f" 等 {len(self.pages)} 页" if len(self.pages) > 4 else ""
            return f"有（{self.count} 处：{where}{extra}）"
        if self.state == "无":
            return "未检出"
        return "不可判（黑白扫描）"


def color_ratio(image_rgb: np.ndarray) -> float:
    """彩色像素占比。黑白扫描件接近 0，彩色扫描件通常在 2% 以上。"""
    if image_rgb.ndim != 3:
        return 0.0
    hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    colored = (sat > 60) & (val > 40)
    return float(colored.mean())


def red_mask(image_rgb: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
    low = cv2.inRange(hsv, (0, 70, 50), (14, 255, 255))
    high = cv2.inRange(hsv, (155, 70, 50), (180, 255, 255))
    return cv2.bitwise_or(low, high)


def detect_seals(image_rgb: np.ndarray, page: int = 1, dpi: int = 120) -> list[SealBox]:
    if image_rgb.ndim != 3:
        return []
    height, width = image_rgb.shape[:2]
    mask = red_mask(image_rgb)
    if not mask.any():
        return []

    # 印章的红环和被环包住的字是分开的几块，先膨胀粘成一体
    kernel_size = max(3, int(round(dpi * 0.05)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    merged = cv2.dilate(mask, kernel, iterations=1)
    merged = cv2.morphologyEx(merged, cv2.MORPH_CLOSE, kernel)

    count, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    boxes: list[SealBox] = []
    min_side = max(24, int(dpi * 0.30))          # 约 9mm，再小是红色文字/标点
    min_red = max(200, int(dpi * dpi * 0.08))    # 真章的红色像素量（4cm 圆章在 120dpi 约 5000）
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if w < min_side or h < min_side:
            continue
        if w > width * MAX_BOX_RATIO or h > height * MAX_BOX_RATIO:
            continue
        ratio = w / h if h else 0
        if not 0.3 <= ratio <= 3.2:               # 细长横条是红头
            continue
        region = mask[y:y + h, x:x + w]
        density = float(region.mean()) / 255 if region.size else 0.0
        if density < MIN_RED_DENSITY:             # 只有零星红点，不是章
            continue
        solid = int(region.sum() / 255)
        if solid < min_red:
            continue
        # 档案扫描件页眉几乎都有红色“档号/序号”表格框，它不是印章。
        # 表格线的特征是：某一行或某一列几乎整条是红的；圆章没有这种直边。
        row_ratio = float((region > 0).sum(axis=1).max()) / w if w else 0.0
        col_ratio = float((region > 0).sum(axis=0).max()) / h if h else 0.0
        if row_ratio >= 0.75 or col_ratio >= 0.75:
            continue
        # 圆章外接框的四角是空的；矩形红框的四角是红的
        corner_w = max(2, int(w * 0.18))
        corner_h = max(2, int(h * 0.18))
        corners = (
            region[:corner_h, :corner_w], region[:corner_h, -corner_w:],
            region[-corner_h:, :corner_w], region[-corner_h:, -corner_w:],
        )
        filled = sum(1 for corner in corners if corner.size and corner.mean() / 255 > 0.12)
        if filled >= 3:
            continue
        boxes.append(SealBox(page=page, x=int(x), y=int(y), w=int(w), h=int(h), area=solid))

    # 同一枚章被拆成几块时合并：中心距离小于章宽的 60% 视为一枚
    boxes.sort(key=lambda item: item.area, reverse=True)
    kept: list[SealBox] = []
    for box in boxes:
        cx, cy = box.x + box.w / 2, box.y + box.h / 2
        duplicate = False
        for other in kept:
            ox, oy = other.x + other.w / 2, other.y + other.h / 2
            if abs(cx - ox) < max(box.w, other.w) * 0.6 and abs(cy - oy) < max(box.h, other.h) * 0.6:
                duplicate = True
                break
        if not duplicate:
            kept.append(box)
    return kept


def scan_seals(pages: list[tuple[int, np.ndarray]], dpi: int = 120) -> SealScan:
    """对若干页做印章检测，返回汇总结果。``pages`` 为 (页码, RGB 图)。"""
    all_boxes: list[SealBox] = []
    ratios: list[float] = []
    for number, image in pages:
        ratios.append(color_ratio(image))
        all_boxes.extend(detect_seals(image, page=number, dpi=dpi))

    ratio = max(ratios) if ratios else 0.0
    if all_boxes:
        pages_hit = sorted({box.page for box in all_boxes})
        return SealScan(state="有", pages=pages_hit, count=len(all_boxes), color_ratio=ratio)
    if not ratios or ratio < COLOR_PAGE_MIN:
        return SealScan(
            state="未知", color_ratio=ratio,
            note="黑白扫描件，红色印章无法判定",
        )
    return SealScan(state="无", color_ratio=ratio, note="彩色页面上未检出红色印章")
