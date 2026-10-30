# 评测/评测4_评测.py
# 读 标注结果.csv，算 P / R / F1：
#   · 生产口径：走 matcher.match()（含文件夹兜底），报系统两个固定工作点
#   · 纯分数口径：只用原始 top-1 分数，5 折留出定阈值（不调生产代码）
#   · 两个基线同口径跑 5 折
import csv, math, random, sys
from pathlib import Path

根目录 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(根目录))

from core.docx_parser import parse_docx
from core.scanner import scan_directory
from core.engine import filter_checkpoints
from core.matcher import Matcher
from core.text import clean_for_match, char_ngrams

标准 = 根目录 / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx"


def 绝对路径(值: str) -> Path:
    p = Path(值)
    return p if p.is_absolute() else 根目录 / p


# 用法：python 评测/评测4_评测.py "工程文件夹" "评测/项目B"
项目目录 = 绝对路径(sys.argv[1]) if len(sys.argv) > 1 else 根目录 / "（AX）项目A（220kV输电线路工程）扫描件"
数据目录 = 绝对路径(sys.argv[2]) if len(sys.argv) > 2 else 根目录 / "评测"
已匹配阈值 = Matcher.MATCH_THRESHOLD      # 0.62
疑似阈值 = Matcher.SUSPECT_THRESHOLD      # 0.40

cpset = parse_docx(标准)
files = scan_directory(项目目录)
cps = filter_checkpoints(cpset.checkpoints, "8", "82", "施工")
ids = [cp.id for cp in cps]

标注 = {}
with open(数据目录 / "标注结果.csv", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        s = 标注.setdefault(r["cp_id"], set())
        if r["标注"].strip() == "1":
            s.add(r["候选文件"])
缺 = [i for i in ids if i not in 标注]
if 缺:
    raise SystemExit(f"标注结果里缺 {len(缺)} 条检查点，例如 {缺[:3]}")

项目名 = 项目目录.name


def 造() -> Matcher:
    return Matcher(cps, project_name=项目名, known_codes=[c.code for c in cpset.checkpoints])


# 生产口径：和 engine.py 完全一致（match 内含文件夹兜底）
生产 = {r.checkpoint.id: (r.score, r.evidence[0].rel_path if r.evidence else None)
        for r in 造().match(files)}

# 纯分数口径：原始 top-1（不看文件夹兜底）
matcher = 造()
matcher.set_file_context(files)
最好 = {}
for f in files:
    for s in matcher.score_file(f):
        if s.index not in 最好 or s.score > 最好[s.index][0]:
            最好[s.index] = (s.score, f.rel_path)
原始 = {cps[i].id: 最好.get(i, (0.0, None)) for i in range(len(cps))}

文件名清洗 = {f.rel_path: clean_for_match(f.name) for f in files}
文件名三克 = {f.rel_path: char_ngrams(文件名清洗[f.rel_path], 3) for f in files}
三克 = {}
for cp in cps:
    a = char_ngrams(clean_for_match(cp.text), 3)
    高 = (0.0, None)
    for f in files:
        b = 文件名三克[f.rel_path]
        sim = len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0
        if sim > 高[0]:
            高 = (sim, f.rel_path)
    三克[cp.id] = 高

精确 = {}
for cp in cps:
    t = clean_for_match(cp.text)
    命中 = next((f.rel_path for f in files if clean_for_match(f.stem) == t), None)
    精确[cp.id] = (1.0 if 命中 else 0.0, 命中)


def 计数(预测, 阈值, 子集):
    tp = sum(1 for i in 子集 if 预测[i][0] >= 阈值 and 预测[i][1] in 标注[i])
    m = sum(1 for i in 子集 if 预测[i][0] >= 阈值)
    t = sum(1 for i in 子集 if 标注[i])
    return tp, m, t


def 指标(tp, m, t):
    P = tp / m if m else 0.0
    R = tp / t if t else 0.0
    return P, R, (2 * P * R / (P + R) if P + R else 0.0)


def 定阈值(预测, 子集, 起=30):
    return max((指标(*计数(预测, θ / 100, 子集))[2], θ / 100) for θ in range(起, 96))[1]


def 交叉验证(预测, 起=30, 折数=5, 种子=7):
    顺序 = list(ids)
    random.Random(种子).shuffle(顺序)
    桶 = [顺序[i::折数] for i in range(折数)]
    TP = M = T = 0
    阈值们 = []
    for k in range(折数):
        训练 = [i for j, b in enumerate(桶) if j != k for i in b]
        θ = 定阈值(预测, 训练, 起)
        阈值们.append(θ)
        tp, m, t = 计数(预测, θ, 桶[k])
        TP, M, T = TP + tp, M + m, T + t
    return 指标(TP, M, T), 阈值们


有答案 = sum(1 for i in ids if 标注[i])
print(f"项目：{项目目录}")
print(f"共 {len(ids)} 条检查点：有正确答案 {有答案} 条，其余 {len(ids) - 有答案} 条标了没有/存疑")
print("提醒：没有答案的条目里，可能有「文件其实存在但没进候选池」的，所以 R 的分母偏小、R 略偏高。")
print()

print("=== 生产口径：系统两个工作点（固定阈值，不调参）===")
for 名, θ in [("已匹配", 已匹配阈值), ("已匹配+疑似", 疑似阈值)]:
    tp, m, t = 计数(生产, θ, ids)
    P, R, F1 = 指标(tp, m, t)
    print(f"{名:<12}≥{θ:.2f}  P={P:.1%}  R={R:.1%}  F1={F1:.1%}  判定={m}/{t}")

print()
print(f"=== 5 折留出（每折在另外 4 折定阈值，只在本折报成绩）===")
print(f"{'方法':<14}{'阈值中位':>8}{'阈值范围':>14}{'P':>9}{'R':>9}{'F1':>9}")
for 名, 预测, 起 in [("系统·含兜底", 生产, 40), ("系统·纯分数", 原始, 30),
                     ("三克基线", 三克, 30), ("精确基线", 精确, 30)]:
    (P, R, F1), θs = 交叉验证(预测, 起)
    θs = sorted(θs)
    print(f"{名:<14}{θs[len(θs) // 2]:>8.2f}{f'{θs[0]:.2f}–{θs[-1]:.2f}':>14}"
          f"{P:>8.1%}{R:>9.1%}{F1:>9.1%}")

# ---------- 明细，给第 6 步错误分析用 ----------
出 = 数据目录 / "预测明细.csv"
误配 = 漏报 = 空判成有 = 0
with open(出, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["cp_id", "检查点", "系统分", "系统top1", "原始分", "原始top1", "标注正确文件", "系统判定"])
    for cp in cps:
        sc, top = 生产[cp.id]
        raw_sc, raw_top = 原始[cp.id]
        if sc >= 已匹配阈值 and top not in 标注[cp.id]:
            判 = "误配"
            误配 += 1
        elif 标注[cp.id] and sc < 疑似阈值:
            判 = "漏报"
            漏报 += 1
        elif sc >= 已匹配阈值:
            判 = "命中"
        else:
            判 = ""
        if not 标注[cp.id] and sc >= 已匹配阈值:
            空判成有 += 1
        w.writerow([cp.id, cp.text, f"{sc:.3f}", top or "", f"{raw_sc:.3f}", raw_top or "",
                    " | ".join(sorted(标注[cp.id])), 判])

print()
print(f"明细已写 {出}")
print(f"误配（≥0.62 但 top1 不在标注里）{误配} 条；漏报（有答案但分 <0.40）{漏报} 条；"
      f"没有答案却被判成「已匹配」{空判成有} 条")
