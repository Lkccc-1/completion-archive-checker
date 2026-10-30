# 评测/评测7_跨项目.py
# 真正的留出：在 A 项目上选阈值，原样套到 B 项目上报成绩。
# 同时报固定 0.62 / 0.40 两个工作点，方便和项目内 5 折对照。
import csv, random, statistics, sys
from pathlib import Path

根目录 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(根目录))

from core.docx_parser import parse_docx
from core.scanner import scan_directory
from core.engine import filter_checkpoints
from core.matcher import Matcher

标准 = 根目录 / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx"
项目们 = {
    "项目A": 根目录 / "（AX）项目A（220kV输电线路工程）扫描件",
    "项目B": 根目录 / "项目B（110kV输变电工程）",
}
数据们 = {
    "项目A": 根目录 / "评测",
    "项目B": 根目录 / "评测" / "项目B",
}


def 载入(名: str):
    项目 = 项目们[名]
    数据 = 数据们[名]
    cpset = parse_docx(标准)
    files = scan_directory(项目)
    cps = filter_checkpoints(cpset.checkpoints, "8", "82", "施工")
    ids = [cp.id for cp in cps]

    标注 = {}
    with open(数据 / "标注结果.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            s = 标注.setdefault(r["cp_id"], set())
            if r["标注"].strip() == "1":
                s.add(r["候选文件"])
    缺 = [i for i in ids if i not in 标注]
    if 缺:
        raise SystemExit(f"{名} 标注结果缺 {len(缺)} 条检查点，例如 {缺[:3]}")

    matcher = Matcher(cps, project_name=项目.name,
                      known_codes=[c.code for c in cpset.checkpoints])
    预测 = {r.checkpoint.id: (r.score, r.evidence[0].rel_path if r.evidence else None)
            for r in matcher.match(files)}
    return ids, 标注, 预测


def 计数(预测, 标注, ids, θ):
    tp = sum(1 for i in ids if 预测[i][0] >= θ and 预测[i][1] in 标注[i])
    m = sum(1 for i in ids if 预测[i][0] >= θ)
    t = sum(1 for i in ids if 标注[i])
    return tp, m, t


def 指标(预测, 标注, ids, θ):
    tp, m, t = 计数(预测, 标注, ids, θ)
    P = tp / m if m else 0.0
    R = tp / t if t else 0.0
    return P, R, (2 * P * R / (P + R) if P + R else 0.0), m, t


def 定阈值(预测, 标注, ids):
    return max((指标(预测, 标注, ids, θ / 100)[2], θ / 100)
               for θ in range(30, 96))[1]


def 五折阈值(预测, 标注, ids, 折数=5, 种子=7):
    顺序 = list(ids)
    random.Random(种子).shuffle(顺序)
    桶 = [顺序[i::折数] for i in range(折数)]
    阈值们 = []
    for k in range(折数):
        训练 = [i for j, b in enumerate(桶) if j != k for i in b]
        阈值们.append(定阈值(预测, 标注, 训练))
    return 阈值们


数据 = {名: 载入(名) for 名 in 项目们}
for 名, (ids, 标注, 预测) in 数据.items():
    有答案 = sum(1 for i in ids if 标注[i])
    print(f"{名}：{len(ids)} 条检查点，有答案 {有答案} 条，"
          f"1 文件 {sum(len(v) for v in 标注.values())} 个")

结果 = []
print()
print(f"{'训练→测试':<12}{'阈值来源':<20}{'阈值':>6}{'P':>8}{'R':>8}{'F1':>8}{'判定':>8}")
for 训练, 测试 in [("项目A", "项目B"), ("项目B", "项目A")]:
    θs = 五折阈值(数据[训练][2], 数据[训练][1], 数据[训练][0])
    θ = statistics.median(θs)
    for 来源, 用θ in [
        (f"{训练} 5 折中位", θ),
        ("固定 0.62", 0.62),
        ("固定 0.40", 0.40),
        (f"{测试} 自身 5 折（对照）",
         statistics.median(五折阈值(数据[测试][2], 数据[测试][1], 数据[测试][0]))),
    ]:
        P, R, F1, m, t = 指标(数据[测试][2], 数据[测试][1], 数据[测试][0], 用θ)
        结果.append((f"{训练}→{测试}", 来源, 用θ, P, R, F1, m, t))
        print(f"{训练+'→'+测试:<12}{来源:<20}{用θ:>6.2f}{P:>7.1%}{R:>8.1%}{F1:>8.1%}{m:>5}/{t}")

出 = 根目录 / "评测" / "跨项目结果.csv"
with open(出, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["方向", "阈值来源", "阈值", "P", "R", "F1", "判定", "应找"])
    for 方向, 来源, θ, P, R, F1, m, t in 结果:
        w.writerow([方向, 来源, f"{θ:.2f}", f"{P:.1%}", f"{R:.1%}", f"{F1:.1%}", m, t])
print()
print("已写", 出)
