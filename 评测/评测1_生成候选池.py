# 评测/评测1_生成候选池.py    （从哪儿运行都行，路径按脚本位置算）
import csv, math, random, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.docx_parser import parse_docx
from core.scanner import scan_directory
from core.engine import filter_checkpoints
from core.matcher import Matcher
from core.text import clean_for_match, char_ngrams

根目录 = Path(__file__).resolve().parents[1]   # 项目根目录，和从哪儿运行无关
标准 = 根目录 / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx"


def 绝对路径(值: str) -> Path:
    p = Path(值)
    return p if p.is_absolute() else 根目录 / p


# 用法：python 评测/评测1_生成候选池.py "工程文件夹" "评测/项目B"
# 不传参数时，保持原来的项目A / 评测 目录，兼容旧流程
项目目录 = 绝对路径(sys.argv[1]) if len(sys.argv) > 1 else 根目录 / "（AX）项目A（220kV输电线路工程）扫描件"
数据目录 = 绝对路径(sys.argv[2]) if len(sys.argv) > 2 else 根目录 / "评测"
数据目录.mkdir(parents=True, exist_ok=True)

资料类型, 项目类型, 单位 = "8", "82", "施工"

每条上限 = 15   # 每条检查点最多给几个候选：通用词（如"质量证明文件"）会命中上百个，不收口标不完

cpset = parse_docx(标准)
files = scan_directory(项目目录)
cps = filter_checkpoints(cpset.checkpoints, 资料类型, 项目类型, 单位)

# 第一路：系统自己的排序（和 engine.py 一致：传项目名 + 先算公共前缀，否则和线上排序对不上）
matcher = Matcher(cps, project_name=项目目录.name, known_codes=[c.code for c in cpset.checkpoints])
matcher.set_file_context(files)
系统 = {i: [] for i in range(len(cps))}
for f in files:
    for s in matcher.score_file(f):
        系统[s.index].append((s.score, f.rel_path))

# 文件名只清洗/切一次（251 条检查点共用）
文件名清洗 = {f.rel_path: clean_for_match(f.name) for f in files}
文件名三克 = {f.rel_path: char_ngrams(文件名清洗[f.rel_path], 3) for f in files}

rows = []
被截断 = 0
for i, cp in enumerate(cps):
    # 第二路：字符 3-gram 相似度（只看文件名）
    a = char_ngrams(clean_for_match(cp.text), 3)
    三克 = []
    for f in files:
        b = 文件名三克[f.rel_path]
        sim = len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0
        三克.append((sim, f.rel_path))
    三克.sort(reverse=True)

    # 三路并集，去重，打乱顺序（打乱是为了不让你被系统的排序带着走）
    候选 = []

    def 加(p):
        if p not in 候选:
            候选.append(p)

    for _, p in sorted(系统[i], reverse=True)[:5]:
        加(p)
    for _, p in 三克[:5]:
        加(p)

    # 第三路：检查点文字是文件名子串。命中过多时按 3-gram 分数从高到低补，补满上限为止
    t = clean_for_match(cp.text)
    子串 = [p for _, p in 三克 if t and t in 文件名清洗[p]]
    for p in 子串:
        if len(候选) >= 每条上限:
            被截断 += 1
            break
        加(p)

    random.seed(7); random.shuffle(候选)

    for p in 候选:
        rows.append({"cp_id": cp.id, "检查点": cp.text, "候选文件": p,
                     "标注": "", "备注": ""})

if not rows:
    raise SystemExit(f"没扫到候选：检查 项目目录 是否存在、里面有没有文件 → {项目目录}")

出 = 数据目录 / "候选池.csv"
with open(出, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)

数 = {}
for r in rows:
    数[r["cp_id"]] = 数.get(r["cp_id"], 0) + 1
print(f"项目：{项目目录}")
print(f"共 {len(rows)} 行候选 / {len(数)} 条检查点，"
      f"每条 {min(数.values())}–{max(数.values())} 个；{被截断} 条检查点触顶截断")
print("去", 出, "里标")
