"""生成一套完全虚构的示例数据，供公开仓库演示用。

产出：
    归档范围示例（合成数据）.docx     12 列归档范围表，27 条检查点
    示例工程（合成数据）/              若干文件夹 + 文本层 PDF

所有工程名、文件内容均为虚构，不含任何真实项目信息。
用法：python 工具/生成示例数据.py
"""
from __future__ import annotations

from pathlib import Path

import fitz
from docx import Document
from docx.shared import Pt

BASE = Path(__file__).resolve().parents[1]
DOCX = BASE / "归档范围示例（合成数据）.docx"
PROJECT = BASE / "示例工程（合成数据）"

HEADER = ["分类号", "类目名称", "归档内容", "主要项目文件", "执行标准", "来源",
          "立卷", "建设", "设计", "监理", "施工", "备注"]

# (分类号, 类目名称, 立卷, 检查点文本)
ROWS: list[tuple[str, str, str, str]] = [
    # 8200 赔偿文件
    ("8200", "赔偿文件", "施工", "土地权属证明文件"),
    ("8200", "赔偿文件", "施工", "占地协议"),
    ("8200", "赔偿文件", "施工", "赔偿明细表、汇总表"),
    ("8200", "赔偿文件", "施工", "赔偿收据"),
    # 8230 施工管理
    ("8230", "施工管理", "施工", "工程开工报审表及附件"),
    ("8230", "施工管理", "施工", "施工组织设计报审表"),
    ("8230", "施工管理", "施工", "安全技术交底记录"),
    ("8230", "施工管理", "施工", "工作联系单"),
    # 8231 基础工程
    ("8231", "基础工程", "施工", "基础分部工程开工报告"),
    ("8231", "基础工程", "施工", "基础工程质量验收记录"),
    ("8231", "基础工程", "施工", "基础混凝土施工记录"),
    ("8231", "基础工程", "施工", "钢筋进场检验报告"),
    # 8232 杆塔工程
    ("8232", "杆塔工程", "施工", "杆塔组立分部工程开工报告"),
    ("8232", "杆塔工程", "施工", "杆塔组立质量验收评级记录"),
    ("8232", "杆塔工程", "施工", "铁塔进场检验报告"),
    ("8232", "杆塔工程", "施工", "高塔组立专项施工方案"),
    # 8233 架线工程
    ("8233", "架线工程", "施工", "架线分部工程开工报告"),
    ("8233", "架线工程", "施工", "导地线液压压接施工记录"),
    ("8233", "架线工程", "施工", "导线、地线开箱检查记录"),
    # 8234 接地工程
    ("8234", "接地工程", "施工", "接地装置隐蔽工程验收记录"),
    ("8234", "接地工程", "施工", "接地电阻测试报告"),
    # 8235 竣工验收
    ("8235", "竣工验收", "施工", "竣工预验收报告"),
    ("8235", "竣工验收", "施工", "工程质量评估报告"),
    ("8235", "竣工验收", "施工", "竣工资料移交清单"),
    # 8236 竣工图
    ("8236", "竣工图", "施工", "杆塔明细表（竣工图）"),
    ("8236", "竣工图", "施工", "基础竣工图"),
    ("8236", "竣工图", "施工", "线路平断面图"),
]

# 文件夹 -> [(文件名, 是否生成)]；不生成的就是“缺失”
LAYOUT: dict[str, list[str]] = {
    "AX-8200-1 赔偿文件": ["1.土地权属证明文件.pdf", "2.占地协议.pdf", "4.赔偿收据（复印件）.pdf"],
    "AX-8230-1 施工管理": ["1.工程开工报审表及附件.pdf", "2.施工组织设计报审表.pdf", "4.工作联系单.pdf"],
    "AX-8231-1 基础工程": ["1.基础分部工程开工报告.pdf", "2.基础工程质量验收记录.pdf",
                          "3.基础混凝土施工记录.pdf"],
    "AX-8232-1 杆塔工程": ["1.杆塔组立分部工程开工报告.pdf", "2.杆塔组立质量验收评级记录.pdf",
                          "3.铁塔进场检验报告.pdf", "4.高塔组立专项施工方案.pdf"],
    "AX-8233-1 架线工程": ["1.架线分部工程开工报告.pdf", "2.导地线压接记录.pdf",
                          "3.导线开箱记录.pdf"],
    # 接地工程整卷缺失：用来演示“整类缺失”提示
    "AX-8235-1 竣工验收": ["2.工程质量评估报告.pdf", "3.竣工资料移交清单.pdf"],
    "AX-8236-1 竣工图": ["1.杆塔明细表（竣工图）.pdf", "2.基础竣工图.pdf"],
    "AX-8299-1 其他": ["1.现场照片.pdf", "2.会议签到表.pdf"],
}


def build_docx() -> None:
    doc = Document()
    doc.core_properties.title = "归档范围示例（合成数据）"
    doc.add_paragraph("归档范围示例（合成数据）")
    doc.add_paragraph("本表为演示用虚构数据，不对应任何真实工程或标准。")

    table = doc.add_table(rows=1, cols=12)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, HEADER):
        cell.text = text
        for run in cell.paragraphs[0].runs:
            run.bold = True
            run.font.size = Pt(9)

    def add(code: str, name: str, archive: str, files: str, filing: str) -> None:
        cells = table.add_row().cells
        values = [code, name, archive, files, "", "施工单位", filing, "", "", "", "30年", ""]
        for cell, value in zip(cells, values):
            cell.text = value

    add("8", "基本建设", "", "", "")
    add("82", "交流输电线路", "", "", "")
    for level3, name3 in [("820", "项目准备"), ("823", "项目施工")]:
        add(level3, name3, "", "", "")

    for i, (code, category, filing, text) in enumerate(ROWS, start=1):
        add(code, category, category, f"{i}. {text}", filing)

    doc.save(str(DOCX))


# 印章三档要能演示出来，样本里必须有三种页面：
#   有章（红圈）→ 检出印章；彩色页无红章 → 未见印章；纯黑白 → 不可判
SEALED = {
    "1.工程开工报审表及附件.pdf",
    "2.施工组织设计报审表.pdf",
    "2.基础工程质量验收记录.pdf",
    "2.杆塔组立质量验收评级记录.pdf",
    "1.杆塔明细表（竣工图）.pdf",
    "3.竣工资料移交清单.pdf",
}
COLORED = {"2.占地协议.pdf", "3.导线开箱记录.pdf"}


def make_pdf(path: Path, title: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), title, fontname="china-s", fontsize=16)
    page.insert_text((72, 130), "示例工程 · 合成数据，仅用于演示", fontname="china-s", fontsize=10)

    if path.name in SEALED:
        # 4cm 圆章的尺寸量级：半径约 2cm、线宽 6pt、章内带红字
        centre, radius = fitz.Point(430, 690), 57
        page.draw_circle(centre, radius, color=(0.85, 0.1, 0.12), width=6)
        page.insert_text((centre.x - 40, centre.y + 5), "DEMO SEAL",
                         fontsize=11, color=(0.85, 0.1, 0.12))
    elif path.name in COLORED:
        # 有彩色但没红章：用来演示「彩色页面上未检出红色印章」
        page.draw_rect(fitz.Rect(72, 60, 520, 90), color=None, fill=(0.12, 0.32, 0.62))

    doc.save(str(path))
    doc.close()


def build_project() -> None:
    for folder, files in LAYOUT.items():
        directory = PROJECT / folder
        directory.mkdir(parents=True, exist_ok=True)
        for name in files:
            make_pdf(directory / name, name.rsplit(".", 1)[0])


def main() -> None:
    build_docx()
    build_project()
    n = sum(len(v) for v in LAYOUT.values())
    print(f"已生成 {DOCX.name}（{len(ROWS)} 条检查点）")
    print(f"已生成 {PROJECT.name}：{len(LAYOUT)} 个文件夹 / {n} 份 PDF")


if __name__ == "__main__":
    main()
