# 评测/评测6_消融.py
# 第 5 步：关掉同义词表 / 文书类型族，看 F1 掉多少。阈值固定 0.40（第 4 步定的），四组同口径。
import csv, sys
from pathlib import Path

根目录 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(根目录))

from core.docx_parser import parse_docx
from core.scanner import scan_directory
from core.engine import filter_checkpoints
import core.matcher as mm

标准 = 根目录 / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx"


def 绝对路径(值: str) -> Path:
    p = Path(值)
    return p if p.is_absolute() else 根目录 / p


# 用法：python 评测/评测6_消融.py "工程文件夹" "评测/项目B"
项目目录 = 绝对路径(sys.argv[1]) if len(sys.argv) > 1 else 根目录 / "（AX）项目A（220kV输电线路工程）扫描件"
数据目录 = 绝对路径(sys.argv[2]) if len(sys.argv) > 2 else 根目录 / "评测"
阈值 = 0.40

cpset = parse_docx(标准)
files = scan_directory(项目目录)
cps = filter_checkpoints(cpset.checkpoints, "8", "82", "施工")

标注 = {}
with open(数据目录 / "标注结果.csv", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        s = 标注.setdefault(r["cp_id"], set())
        if r["标注"].strip() == "1":
            s.add(r["候选文件"])

原同义词 = mm.Matcher.SYNONYMS
原类型族 = mm.DOC_FAMILIES


def 跑一次(用同义词: bool, 用类型族: bool):
    mm.Matcher.SYNONYMS = 原同义词 if 用同义词 else ()
    mm.DOC_FAMILIES = 原类型族 if 用类型族 else ()
    matcher = mm.Matcher(cps, project_name=项目目录.name,
                         known_codes=[c.code for c in cpset.checkpoints])
    tp = m = t = 0
    for r in matcher.match(files):
        cid = r.checkpoint.id
        if 标注[cid]:
            t += 1
        if r.score >= 阈值:
            m += 1
            if r.evidence and r.evidence[0].rel_path in 标注[cid]:
                tp += 1
    P = tp / m if m else 0.0
    R = tp / t if t else 0.0
    return P, R, (2 * P * R / (P + R) if P + R else 0.0), m, t


配置 = [
    ("完整", True, True),
    ("去掉同义词表", False, True),
    ("去掉文书类型族", True, False),
    ("两个都去掉", False, False),
]
结果 = []
for 名, 同义, 族 in 配置:
    P, R, F1, m, t = 跑一次(同义, 族)
    结果.append((名, P, R, F1, m, t))
    print(f"{名:<10} P={P:.1%}  R={R:.1%}  F1={F1:.1%}  判定={m}/{t}")

mm.Matcher.SYNONYMS = 原同义词
mm.DOC_FAMILIES = 原类型族

基准 = 结果[0][3]
出 = 数据目录 / "消融结果.csv"
with open(出, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["配置", "P", "R", "F1", "F1变化", "判定", "应找"])
    for 名, P, R, F1, m, t in 结果:
        w.writerow([名, f"{P:.1%}", f"{R:.1%}", f"{F1:.1%}",
                    "—" if 名 == "完整" else f"{(F1 - 基准) * 100:+.1f} 个百分点", m, t])
print()
print("-- 贡献（完整 F1 减去缺件后的 F1）--")
for 名, P, R, F1, m, t in 结果[1:]:
    print(f"{名}：{(基准 - F1) * 100:+.1f} 个百分点")
print("已写", 出)
