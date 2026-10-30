"""竣工资料完整性检查器 · Streamlit 入口。

流程：上传/加载模版 → 选择目录并扫描 → 检查 → 人工纠正 → 导出 Excel。
"""
from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from core.engine import check, content_available, detect_project_types, reapply_overrides
from core.exporter import export_report
from core.models import (
    DOC_TYPES,
    STATE_MATCHED,
    STATE_MISSING,
    STATE_NA,
    STATE_SUSPECT,
    UNITS,
    CheckReport,
    CheckpointSet,
)
from core.overrides import (
    OverrideStore,
    project_dir,
    save_config,
    slugify,
)
from core.scanner import DEFAULT_EXTENSIONS, scan_directory
from core.template_loader import load_template
from core.yaml_loader import dump_checkpoint_set

BASE_DIR = Path(__file__).resolve().parent
MANUAL_DIR = "（手动输入）"
MISSING_CHOICES = ["暂不处理", "确认缺失", "已提供（改判已匹配）", "不适用"]
SUSPECT_CHOICES = ["待确认", "确认缺失", "已提供（改判已匹配）", "不适用"]

st.set_page_config(
    page_title="竣工资料完整性检查器",
    page_icon="📑",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 配色：深墨蓝（工程/档案的专业感）+ 中性冷灰底，状态色只用于状态本身
COLOR = {
    "ink": "#16324F",
    "ink_soft": "#4A6076",
    "line": "#E2E6EC",
    "bg": "#F5F6F8",
    "surface": "#FFFFFF",
    "matched": "#2C7A5B",
    "suspect": "#B0761A",
    "missing": "#B03A2E",
    "na": "#78869A",
}

st.markdown(
    """
    <style>
    :root {
      --ink: #16324F; --ink-soft: #4A6076; --line: #E2E6EC; --bg: #F5F6F8;
      --surface: #FFFFFF; --matched: #2C7A5B; --suspect: #B0761A;
      --missing: #B03A2E; --na: #78869A;
    }
    html, body, [class*="css"], .stApp {
      font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    }
    .stApp { background: var(--bg); }
    .block-container { padding-top: 1.2rem; padding-bottom: 3.5rem; max-width: 1480px; }

    /* 顶部标题带 */
    .app-header {
      display: flex; align-items: flex-end; justify-content: space-between;
      gap: 24px; padding: 4px 0 18px; border-bottom: 1px solid var(--line);
      margin-bottom: 20px;
    }
    .app-header h1 {
      font-size: 1.62rem; font-weight: 700; color: var(--ink);
      margin: 0 0 6px; letter-spacing: .01em; line-height: 1.25;
    }
    .app-header .sub { color: var(--ink-soft); font-size: .9rem; margin: 0; }
    .chip-row { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
    .chip {
      background: var(--surface); border: 1px solid var(--line); border-radius: 999px;
      padding: 5px 12px; font-size: .78rem; color: var(--ink-soft); white-space: nowrap;
    }
    .chip b { color: var(--ink); font-weight: 600; }

    /* KPI 卡片 */
    .kpi-row { display: flex; gap: 12px; margin: 4px 0 6px; }
    .kpi {
      flex: 1; background: var(--surface); border: 1px solid var(--line);
      border-radius: 10px; padding: 14px 16px 13px; position: relative; overflow: hidden;
    }
    .kpi::before {
      content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
      background: var(--line);
    }
    .kpi.matched::before { background: var(--matched); }
    .kpi.suspect::before { background: var(--suspect); }
    .kpi.missing::before { background: var(--missing); }
    .kpi.na::before { background: var(--na); }
    .kpi.ink::before { background: var(--ink); }
    .kpi-num {
      font-size: 1.72rem; font-weight: 700; color: var(--ink); line-height: 1.1;
      font-variant-numeric: tabular-nums; letter-spacing: -.01em;
    }
    .kpi.matched .kpi-num { color: var(--matched); }
    .kpi.suspect .kpi-num { color: var(--suspect); }
    .kpi.missing .kpi-num { color: var(--missing); }
    .kpi.na .kpi-num { color: var(--na); }
    .kpi-label { font-size: .78rem; color: var(--ink-soft); margin-top: 5px; }

    /* 完整率条 */
    .rate-wrap { background: var(--surface); border: 1px solid var(--line);
      border-radius: 10px; padding: 16px 18px; margin: 10px 0 4px; }
    .rate-head { display: flex; justify-content: space-between; align-items: baseline;
      font-size: .82rem; color: var(--ink-soft); margin-bottom: 10px; }
    .rate-head b { font-size: 1.28rem; color: var(--ink); font-variant-numeric: tabular-nums; }
    .rate-track { height: 9px; background: #EDF0F4; border-radius: 999px; overflow: hidden; }
    .rate-fill { height: 100%; border-radius: 999px;
      background: linear-gradient(90deg, #2C7A5B, #4C9C77); }

    /* 分类完整率 */
    .cat-row { display: grid; grid-template-columns: 190px 1fr 64px 96px;
      gap: 12px; align-items: center; padding: 7px 0; border-bottom: 1px dashed #EDF0F4; }
    .cat-row:last-child { border-bottom: none; }
    .cat-name { font-size: .84rem; color: var(--ink); overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap; }
    .cat-code { color: var(--ink-soft); font-variant-numeric: tabular-nums; margin-right: 6px; }
    .cat-track { height: 7px; background: #EDF0F4; border-radius: 999px; overflow: hidden; }
    .cat-fill { height: 100%; border-radius: 999px; background: #3E7CB1; }
    .cat-fill.low { background: var(--missing); }
    .cat-fill.mid { background: var(--suspect); }
    .cat-pct { font-size: .8rem; color: var(--ink); text-align: right;
      font-variant-numeric: tabular-nums; }
    .cat-count { font-size: .76rem; color: var(--ink-soft); text-align: right;
      font-variant-numeric: tabular-nums; }

    /* 提示条 */
    .hint { background: var(--surface); border: 1px solid var(--line);
      border-left: 3px solid var(--ink); padding: .75rem 1rem; border-radius: 6px;
      color: #33465A; font-size: .86rem; line-height: 1.6; }
    .hint b { color: var(--ink); }
    .section-title { font-size: .95rem; font-weight: 600; color: var(--ink);
      margin: 22px 0 10px; display: flex; align-items: center; gap: 8px; }
    .section-title.plain::before { content: ""; width: 4px; height: 14px;
      background: var(--ink); border-radius: 2px; display: inline-block; }

    /* 空状态引导 */
    .onboard { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 8px; }
    .step-card { background: var(--surface); border: 1px solid var(--line);
      border-radius: 10px; padding: 16px 18px; }
    .step-card .step-no { display: inline-flex; align-items: center; justify-content: center;
      width: 22px; height: 22px; border-radius: 50%; background: #EAF0F6; color: var(--ink);
      font-size: .75rem; font-weight: 700; margin-bottom: 10px; }
    .step-card h4 { margin: 0 0 6px; font-size: .9rem; color: var(--ink); font-weight: 600; }
    .step-card p { margin: 0; font-size: .82rem; color: var(--ink-soft); line-height: 1.6; }
    .step-badge { display: inline-flex; align-items: center; justify-content: center;
      width: 20px; height: 20px; border-radius: 6px; background: #EAF0F6;
      color: var(--ink); font-size: .72rem; font-weight: 700; margin-right: 6px; }

    /* 控件 */
    .stButton > button { border-radius: 7px; border: 1px solid var(--line);
      font-weight: 600; font-size: .86rem; }
    .stButton > button[kind="primary"] {
      background: var(--ink); border-color: var(--ink); color: #fff;
    }
    .stButton > button[kind="primary"]:hover { background: #1F4368; border-color: #1F4368; }
    [data-testid="stSidebar"] { background: var(--surface); border-right: 1px solid var(--line); }
    [data-testid="stSidebar"] .block-container { padding-top: 1rem; }
    [data-testid="stHeader"] { background: transparent; }
    .stTabs [data-baseweb="tab-list"] { gap: 2px; border-bottom: 1px solid var(--line); }
    .stTabs [data-baseweb="tab"] {
      height: 38px; padding: 0 14px; font-size: .87rem; color: var(--ink-soft);
      background: transparent; border-radius: 6px 6px 0 0;
    }
    .stTabs [aria-selected="true"] { color: var(--ink) !important; font-weight: 600; }
    [data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
    .sb-brand { font-size: 1rem; font-weight: 700; color: var(--ink); letter-spacing: .01em; }
    .sb-sub { font-size: .76rem; color: var(--ink-soft); margin: 2px 0 14px; }
    hr { margin: 1rem 0; border-color: var(--line); }
    </style>
    """,
    unsafe_allow_html=True,
)

DEFAULTS = {
    "template_set": None,
    "template_label": "",
    "template_hash": "",
    "files": [],
    "scan_root": "",
    "detected_types": [],
    "report": None,
    "overrides": {},
    "override_store_path": "",
    "export_bytes": None,
    "export_name": "",
    "yaml_bytes": None,
    "root_text": "",
    "example_dir": MANUAL_DIR,
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _builtin_template_path() -> Path | None:
    direct = BASE_DIR / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx"
    if direct.exists():
        return direct
    for pattern in ("*分类、归档范围及保管期限表.docx", "*归档范围示例*.docx"):
        for candidate in sorted(BASE_DIR.glob(pattern)):
            if candidate.is_file() and not candidate.name.startswith("~$"):
                return candidate
    return None


def _reset_project_type() -> None:
    """换模版/换目录后清掉旧的项目类型选择，避免沿用上一个项目的 82/83。"""
    for key in ("project_type_8", "project_type_9"):
        st.session_state.pop(key, None)


def _switch_doc_type(code: str) -> None:
    """切到另一个资料类型。8 基本建设 / 9 设备仪器 是两套编码，一次只查一套。

    两套的立卷单位也不同（9x 设备类归“建设/物资供应”），当前单位在新类型下
    没有检查点时会自动改到有检查点的那个，否则一点检查就报“没有检查点”。
    """
    st.session_state["doc_type"] = code
    template_set = st.session_state.get("template_set")
    if not template_set:
        return

    files = st.session_state.get("files") or []
    detected = detect_project_types(files, template_set, code) if files else []
    prefix = detected[0][0] if detected else code

    def usable(unit: str) -> bool:
        return any(
            cp.code.startswith(prefix)
            and (unit in cp.filing_units or "相关" in cp.filing_units)
            for cp in template_set.checkpoints
        )

    if not usable(st.session_state.get("unit", UNITS[0])):
        for unit in UNITS:
            if usable(unit):
                st.session_state["unit"] = unit
                break


def _load_template_from_path(path: Path, label: str) -> None:
    checkpoint_set = load_template(path)
    st.session_state.template_set = checkpoint_set
    st.session_state.template_label = label
    st.session_state.template_hash = checkpoint_set.meta.get("source_hash", "")
    st.session_state.report = None
    st.session_state.export_bytes = None
    st.session_state.yaml_bytes = None
    _reset_project_type()


def _load_template_from_upload(uploaded) -> None:
    data = uploaded.getvalue()
    digest = hashlib.sha1(data).hexdigest()
    if digest == st.session_state.template_hash:
        return
    suffix = Path(uploaded.name).suffix.lower()
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        handle.write(data)
        handle.close()
        _load_template_from_path(Path(handle.name), uploaded.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _on_example_change() -> None:
    name = st.session_state.get("example_dir", MANUAL_DIR)
    if name != MANUAL_DIR:
        st.session_state.root_text = str(BASE_DIR / name)
    _reset_project_type()


def _sample_dirs() -> list[str]:
    try:
        return sorted(
            item.name
            for item in BASE_DIR.iterdir()
            if item.is_dir()
            and not item.name.startswith(
                (".", "~", "__pycache__", "core", "ui", "tests", "data", "checkpoints",
                 "pages", "评测", "工具", "public", "docs")
            )
        )
    except OSError:
        return []


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _choice_state(choice: str) -> str | None:
    return {
        "已提供（改判已匹配）": STATE_MATCHED,
        "确认缺失": STATE_MISSING,
        "不适用": STATE_NA,
    }.get(choice)


def _display_choice(result, choices: list[str]) -> str:
    if result.manual:
        if result.state == STATE_MATCHED:
            return "已提供（改判已匹配）"
        if result.state == STATE_MISSING:
            return "确认缺失"
        if result.state == STATE_NA:
            return "不适用"
    return choices[0]


def _evidence_text(result) -> str:
    return result.evidence_text or ""


def _kpi_row(items: list[tuple[str, int, str]]) -> None:
    """items: (标签, 数值, 色调)"""
    cards = "".join(
        f'<div class="kpi {tone}"><div class="kpi-num">{value}</div>'
        f'<div class="kpi-label">{label}</div></div>'
        for label, value, tone in items
    )
    st.markdown(f'<div class="kpi-row">{cards}</div>', unsafe_allow_html=True)


def _rate_bar(rate: float, note: str) -> None:
    percent = max(0.0, min(1.0, rate)) * 100
    st.markdown(
        f"""
        <div class="rate-wrap">
          <div class="rate-head"><span>资料完整率</span><b>{percent:.1f}%</b></div>
          <div class="rate-track"><div class="rate-fill" style="width:{percent:.1f}%"></div></div>
          <div class="rate-head" style="margin:8px 0 0;font-size:.76rem">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _category_bars(categories: list[dict]) -> None:
    rows = []
    for item in categories:
        rate = item["completeness"] * 100
        tone = "low" if rate < 35 else ("mid" if rate < 65 else "")
        rows.append(
            f'<div class="cat-row">'
            f'<div class="cat-name"><span class="cat-code">{item["code"]}</span>{item["category"]}</div>'
            f'<div class="cat-track"><div class="cat-fill {tone}" style="width:{rate:.1f}%"></div></div>'
            f'<div class="cat-pct">{rate:.0f}%</div>'
            f'<div class="cat-count">{item["matched"]}/{item["total"] - item["na"]}</div>'
            f"</div>"
        )
    st.markdown("".join(rows), unsafe_allow_html=True)


def _section(title: str, step: str | None = None) -> None:
    if step:
        st.markdown(
            f'<div class="section-title"><span class="step-badge">{step}</span>{title}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(f'<div class="section-title plain">{title}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------
# 侧边栏
# --------------------------------------------------------------------------
def render_sidebar() -> None:
    with st.sidebar:
        st.markdown(
            '<div class="sb-brand">竣工资料完整性检查器</div>'
            '<div class="sb-sub">模版比对 · 正文核验 · 印章识别 · 整改单导出</div>',
            unsafe_allow_html=True,
        )

        _section("检查标准", "1")
        uploaded = st.file_uploader(
            "上传模版（docx / doc / yaml）",
            type=["docx", "doc", "yaml", "yml"],
            label_visibility="collapsed",
        )
        if uploaded is not None:
            try:
                _load_template_from_upload(uploaded)
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

        builtin = _builtin_template_path()
        if st.button("加载内置模版", width="stretch", disabled=builtin is None):
            try:
                _load_template_from_path(builtin, builtin.name)
                st.success("已加载内置模版")
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

        template_set: CheckpointSet | None = st.session_state.template_set
        if template_set:
            st.success(
                f"已加载：{len(template_set.checkpoints)} 条检查点 / "
                f"{len(template_set.nodes)} 个类目"
            )
            if template_set.meta.get("mode") == "inventory":
                st.info(
                    "这是「文件清单」标准：每一份实际文件是一条检查点，核对的是"
                    "清单与目录是否一致——清单里有、目录里没有的报“缺失”，"
                    "目录里有、清单里没有的进“未归类”。它不是归档范围标准，"
                    "不会替你判断“还该有哪些文件”。"
                )
            with st.expander("解析诊断"):
                st.caption(st.session_state.template_label)
                for warning in template_set.warnings:
                    st.write("· " + warning)

        _section("项目范围", "2")
        doc_type = st.selectbox(
            "资料类型",
            options=["8", "9"],
            format_func=lambda code: f"{code} {DOC_TYPES[code]}",
            index=0,
            key="doc_type",
            help="基本建设与设备仪器是两套编码（8x / 9x），一次只查一套。",
        )

        st.selectbox(
            "示例目录",
            options=[MANUAL_DIR] + _sample_dirs(),
            key="example_dir",
            on_change=_on_example_change,
        )
        root_text = st.text_input(
            "资料目录（本机路径）",
            key="root_text",
            placeholder=r"D:\项目\竣工资料",
        )
        unit = st.radio(
            "责任单位（按立卷列）", UNITS, horizontal=True, index=3, key="unit"
        )
        include_retention = st.checkbox(
            "并入本单位有保管期限的条目",
            value=False,
            help="默认完全按“立卷”列过滤。勾选后，额外纳入本单位保管期限非空、但立卷单位不是本单位的条目。",
        )
        project_name_input = st.text_input(
            "项目名称（可选）",
            placeholder="用于从文件名中剔除项目名，降低噪声",
            key="project_name_input",
        )

        template_set = st.session_state.template_set
        type_options = [("auto", "自动识别（按文件夹编码）")]
        if template_set:
            type_options += [
                (node.code, f"{node.code} {node.name}")
                for node in template_set.project_types(doc_type)
            ]
        type_choice = st.selectbox(
            "项目类型",
            options=type_options,
            format_func=lambda item: item[1],
            key=f"project_type_{doc_type}",
        )

        if st.button("① 扫描目录", type="primary", width="stretch"):
            if not template_set:
                st.error("请先加载检查标准。")
            elif not root_text.strip():
                st.error("请填写资料目录。")
            else:
                progress = st.progress(0, text="正在扫描目录…")
                try:
                    files = scan_directory(
                        root_text,
                        extensions=DEFAULT_EXTENSIONS,
                        progress=lambda done, total: progress.progress(
                            min(1.0, done / total) if total else 0.0,
                            text=f"扫描中 {done}/{total}",
                        ),
                    )
                    st.session_state.files = files
                    st.session_state.scan_root = str(Path(root_text.strip()).resolve())
                    st.session_state.report = None
                    st.session_state.export_bytes = None
                    st.session_state.detected_types = detect_project_types(files, template_set, doc_type)
                    progress.empty()
                    st.success(f"扫描完成：{len(files)} 个文件")
                except Exception as exc:  # noqa: BLE001
                    progress.empty()
                    st.error(str(exc))

        files = st.session_state.files
        if files:
            detected = detect_project_types(files, template_set, doc_type) if template_set else []
            st.session_state.detected_types = detected
            detection_text = "、".join(f"{code}（{count}）" for code, count in detected[:4])
            st.caption(f"已扫描 {len(files)} 个文件" + (f"；编码提示：{detection_text}" if detection_text else ""))

            # 目录编码与手选项目类型不一致时提示，避免整批检查点筛错
            if detected and type_choice[0] != "auto":
                top_code, top_count = detected[0]
                if top_code != type_choice[0]:
                    st.warning(
                        f"目录编码以 {top_code} 为主（{top_count} 个文件），"
                        f"与所选「{type_choice[1]}」不一致，可能筛错了项目类型。"
                        f"建议改选「自动识别」或 {top_code} 开头的项目类型后重新检查。"
                    )

            # 目录里可能同时有另一套编码（8x 基本建设 / 9x 设备仪器）。
            # 当前只查一套，另一套会被整批忽略，必须明说，否则看起来像没扫到。
            for other in [code for code in DOC_TYPES if code != doc_type]:
                other_detected = detect_project_types(files, template_set, other)
                if not other_detected:
                    continue
                other_total = sum(count for _, count in other_detected)
                other_names = "、".join(
                    f"{code}（{count}）" for code, count in other_detected[:4]
                )
                st.info(
                    f"另有 {other_total} 个文件属于「{other} {DOC_TYPES[other]}」，"
                    f"当前资料类型是「{doc_type} {DOC_TYPES[doc_type]}」，"
                    f"这部分本次不参与检查。编码：{other_names}。"
                )
                st.button(
                    f"切换到「{other} {DOC_TYPES[other]}」",
                    key=f"switch_doc_type_{other}",
                    on_click=_switch_doc_type,
                    args=(other,),
                )

        _section("执行检查", "3")

        content_ready, content_error = content_available()
        read_content = st.checkbox(
            "读取 PDF 正文 + 识别红色印章",
            value=content_ready,
            help="先读文字层；扫描件才走本地 OCR（离线、不联网）。读到能确认对应检查点就停止翻页。",
        )
        if read_content and not content_ready:
            st.error(content_error)
        deep_mode = False
        max_pages = 3
        if read_content:
            deep_mode = st.checkbox(
                "深度模式：连未归类文件一起读",
                value=False,
                help="把文件名对不上的文件也读一遍，用正文标题去补“缺失”项。耗时约翻倍。",
            )
            max_pages = st.slider("每份最多读几页", 1, 5, 3, help="读到能确认即提前停止，这里只是上限。")
            st.caption("首次运行约每页 1–2 秒；结果按文件缓存，重复检查几乎瞬时完成。")

        if st.button(
            "② 开始检查",
            type="primary",
            width="stretch",
            disabled=not (template_set and files),
        ):
            root = st.session_state.scan_root or root_text.strip()
            project_name = project_name_input.strip() or Path(root).name
            try:
                store_dir = project_dir(BASE_DIR, root)
                store = OverrideStore(store_dir / "overrides.json")
                overrides = store.load()
                progress = st.progress(0, text="正在按文件名匹配…")
                report = check(
                    template_set,
                    files,
                    doc_type,
                    type_choice[0],
                    unit,
                    include_retention=include_retention,
                    overrides=overrides,
                    project_name=project_name,
                    root=root,
                    progress=lambda done, total: progress.progress(
                        min(1.0, done / total) if total else 0.0,
                        text=f"文件名匹配 {done}/{total}",
                    ),
                    content={
                        "enabled": read_content,
                        "use_ocr": True,
                        "max_pages": max_pages,
                        "deep": deep_mode,
                        "deep_max_files": 60,
                        "check_seal": read_content,
                        "cache_path": str(store_dir / "content_cache.json"),
                    },
                    content_progress=lambda done, total, path: progress.progress(
                        min(1.0, done / total) if total else 0.0,
                        text=f"读正文 {done}/{total}：{Path(path).name[:28]}",
                    ),
                )
                progress.empty()
                st.session_state.report = report
                st.session_state.overrides = overrides
                st.session_state.override_store_path = str(store.path)
                st.session_state.export_bytes = None
                save_config(
                    store_dir / "config.json",
                    {
                        "root": root,
                        "doc_type": doc_type,
                        "project_type": type_choice[1],
                        "unit": unit,
                        "include_retention": include_retention,
                        "project_name": project_name,
                    },
                )
                st.success("检查完成")
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

        report: CheckReport | None = st.session_state.report
        if report:
            meta = report.meta
            content = meta.get("content") or {}
            st.caption(
                f"本次检查点 {meta.get('filtered_total', 0)} 条 / "
                f"未归类文件 {len(report.orphan_files)} 个"
            )
            if content.get("enabled"):
                st.caption(
                    f"正文：读 {content.get('files_read', 0)} 份 / OCR "
                    f"{content.get('ocr_pages', 0)} 页 / 用时 {content.get('seconds', 0):.0f}s"
                )
            elif content.get("error"):
                st.warning(f"正文核查未执行：{content['error']}")

        _section("导出整改单", "4")
        inspector = st.text_input("检查人", key="inspector_input")
        reviewer = st.text_input("复核人", key="reviewer_input")
        due_days = st.number_input("整改期限（天）", min_value=1, max_value=90, value=7, step=1)
        if st.button("生成 Excel 整改单", width="stretch", disabled=report is None):
            try:
                project_name = (
                    report.meta.get("project_name", "") if report else project_name_input
                )
                st.session_state.export_bytes = export_report(
                    report,
                    project_name=project_name,
                    inspector=inspector,
                    reviewer=reviewer,
                    due_days=int(due_days),
                )
                st.session_state.export_name = (
                    f"竣工资料检查整改单_{slugify(project_name)}_{datetime.now():%Y%m%d_%H%M}.xlsx"
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"导出失败：{exc}")

        if st.session_state.export_bytes:
            st.download_button(
                "⬇ 下载 Excel 整改单",
                data=st.session_state.export_bytes,
                file_name=st.session_state.export_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )

        if st.session_state.override_store_path:
            with st.expander("人工纠正"):
                st.caption(f"纠正文件：{st.session_state.override_store_path}")
                if st.button("清空本项目纠正", width="stretch"):
                    OverrideStore(st.session_state.override_store_path).clear()
                    st.session_state.overrides = {}
                    if st.session_state.report:
                        st.session_state.report = reapply_overrides(
                            st.session_state.report, {}
                        )
                    st.session_state.export_bytes = None
                    st.rerun()

        st.divider()
        if st.button("重置本次运行", width="stretch"):
            for key in ("files", "scan_root", "detected_types", "report", "overrides",
                        "override_store_path", "export_bytes", "export_name"):
                st.session_state[key] = DEFAULTS[key]
            st.rerun()

    return doc_type, root_text, unit, include_retention, project_name_input, type_choice


# --------------------------------------------------------------------------
# 结果表
# --------------------------------------------------------------------------
def _result_frame(results, choices: list[str]) -> pd.DataFrame:
    rows = []
    for result in results:
        cp = result.checkpoint
        rows.append(
            {
                # 可操作的列（处理/备注）放最前，避免长表格要横向翻到最右边才能改判
                "编号": cp.id,
                "处理": _display_choice(result, choices),
                "备注": result.note,
                "主要项目文件": cp.text,
                "证据文件": _evidence_text(result),
                "正文": result.content_text if result.content_pages else "",
                "印章": result.seal_text,
                "置信度": result.confidence,
                "说明": result.reason,
                "分类号": cp.code,
                "类目名称": cp.category,
                "归档内容": cp.archive_content,
                "来源": cp.source,
                "立卷": cp.filing,
                "保管期限": cp.retention_text,
            }
        )
    return pd.DataFrame(rows)


def _filtered_results(
    report: CheckReport, states: tuple[str, ...], keyword: str, code_filter: list[str]
):
    results = report.by_state(*states)
    if code_filter:
        wanted = set(code_filter)
        results = [item for item in results if item.checkpoint.code[:3] in wanted]
    keyword = keyword.strip()
    if keyword:
        results = [
            item
            for item in results
            if keyword in item.checkpoint.code
            or keyword in item.checkpoint.category
            or keyword in item.checkpoint.archive_content
            or keyword in item.checkpoint.text
            or keyword in item.evidence_text
        ]
    return results


def _render_editor(
    report: CheckReport,
    results,
    choices: list[str],
    key_prefix: str,
    store_path: str,
) -> None:
    if not results:
        st.info("没有符合条件的记录。")
        return

    frame = _result_frame(results, choices)
    editor_key = f"{key_prefix}_" + hashlib.md5(
        "|".join(
            frame["编号"].tolist()
            + frame["处理"].tolist()
            + frame["正文"].tolist()
            + frame["印章"].tolist()
        ).encode("utf-8")
    ).hexdigest()[:8]

    edited = st.data_editor(
        frame,
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        key=editor_key,
        column_config={
            "编号": st.column_config.TextColumn("编号", disabled=True, width="small"),
            "处理": st.column_config.SelectboxColumn(
                "处理", options=choices, required=True, width="small"
            ),
            "备注": st.column_config.TextColumn("备注", width="small"),
            "主要项目文件": st.column_config.TextColumn("主要项目文件", disabled=True, width="large"),
            "证据文件": st.column_config.TextColumn("证据文件", disabled=True, width="large"),
            "正文": st.column_config.TextColumn("正文", disabled=True, width="small",
                                                help="正文分 / 读取页数 / 来源"),
            "印章": st.column_config.TextColumn("印章", disabled=True, width="small"),
            "置信度": st.column_config.TextColumn("置信度", disabled=True, width="small"),
            "说明": st.column_config.TextColumn("说明", disabled=True, width="medium"),
            "分类号": st.column_config.TextColumn("分类号", disabled=True, width="small"),
            "类目名称": st.column_config.TextColumn("类目名称", disabled=True, width="small"),
            "归档内容": st.column_config.TextColumn("归档内容", disabled=True, width="small"),
            "来源": st.column_config.TextColumn("来源", disabled=True, width="small"),
            "立卷": st.column_config.TextColumn("立卷", disabled=True, width="small"),
            "保管期限": st.column_config.TextColumn("保管期限", disabled=True, width="small"),
        },
    )

    if st.button("保存本次纠正", key=f"save_{key_prefix}"):
        store = OverrideStore(store_path)
        items = store.load()
        base = frame.set_index("编号")
        changed = 0
        for _, row in edited.iterrows():
            cp_id = str(row["编号"])
            choice = str(row["处理"])
            note = str(row.get("备注") or "")
            old = base.loc[cp_id]
            if choice == str(old["处理"]) and note == str(old["备注"]):
                continue
            changed += 1
            state = _choice_state(choice)
            if state is None:
                items.pop(cp_id, None)
            else:
                items[cp_id] = {
                    "state": state,
                    "note": note,
                    "reviewer": st.session_state.get("reviewer_input", ""),
                    "time": datetime.now().isoformat(timespec="seconds"),
                }
        if changed:
            store.save(
                items,
                {"template_hash": report.meta.get("template_hash", "")},
            )
            st.session_state.overrides = store.load()
            st.session_state.report = reapply_overrides(
                report, st.session_state.overrides
            )
            st.session_state.export_bytes = None
            st.toast(f"已保存 {changed} 条纠正")
            st.rerun()
        else:
            st.info("没有检测到改动。")


def _render_overview(report: CheckReport) -> None:
    stats = report.stats
    meta = report.meta
    content = meta.get("content") or {}

    _kpi_row(
        [
            ("检查点", stats["total"], "ink"),
            ("已匹配", stats["matched"], "matched"),
            ("疑似", stats["suspect"], "suspect"),
            ("缺失", stats["missing"], "missing"),
            ("不适用", stats["na"], "na"),
            ("未归类文件", len(report.orphan_files), "ink"),
        ]
    )
    denominator = max(0, stats["total"] - stats["na"])
    _rate_bar(
        stats["completeness"],
        f"已匹配 {stats['matched']} / 应归档 {denominator}（已扣除不适用 {stats['na']} 条）",
    )

    st.markdown(
        '<div class="chip-row" style="justify-content:flex-start;margin:14px 0 4px">'
        f'<span class="chip">资料类型 <b>{meta.get("doc_type_name", "")}</b></span>'
        f'<span class="chip">项目类型 <b>{meta.get("project_type_name", "")}</b></span>'
        f'<span class="chip">责任单位 <b>{meta.get("unit", "")}</b></span>'
        f'<span class="chip">人工纠正 <b>{stats.get("manual", 0)}</b></span>'
        "</div>",
        unsafe_allow_html=True,
    )

    categories = stats.get("by_category", [])
    if categories:
        _section("按三级分类的完整率")
        _category_bars(categories)

    if content.get("enabled"):
        _section("正文与印章核查")
        _kpi_row(
            [
                ("读取文件", content.get("files_read", 0), "ink"),
                ("OCR 页数", content.get("ocr_pages", 0), "ink"),
                ("正文确认", content.get("promoted", 0), "matched"),
                ("正文救回", content.get("rescued", 0), "suspect"),
                ("检出印章", content.get("seals_found", 0), "matched"),
                ("未见印章", content.get("seals_missing", 0), "missing"),
                ("印章不可判", content.get("seals_unknown", 0), "na"),
            ]
        )
        st.caption(
            f"用时 {content.get('seconds', 0):.0f} 秒；提前停止 {content.get('early_stops', 0)} 次；"
            f"命中缓存 {content.get('cache_hits', 0)} 份。"
        )

    _section("整类缺失")
    if report.category_gaps:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "分类号": gap.code,
                        "类目名称": gap.category,
                        "缺失条数": gap.count,
                        "层级路径": " > ".join(gap.path),
                    }
                    for gap in report.category_gaps
                ]
            ),
            hide_index=True,
            width="stretch",
            height=min(420, 45 + 35 * len(report.category_gaps)),
        )
    else:
        st.success("未发现整类缺失。")

    if content.get("enabled"):
        note = (
            "文件名为主证据，正文为补充证据：正文确认过的“疑似”已升级为“已匹配”，"
            "正文救回的空白项仍记“疑似”供开卷确认。印章识别只认红色印章，黑白扫描件判为“不可判”。"
        )
    else:
        note = (
            "<b>本期未读取文件正文</b>，只按文件名和文件夹路径匹配。"
            "如需提升准确率，可在左侧勾选“读取 PDF 正文 + 识别红色印章”。"
        )
    st.markdown(f'<div class="hint">{note}</div>', unsafe_allow_html=True)


def _render_report(report: CheckReport, template_set: CheckpointSet) -> None:
    stats = report.stats
    seal_total = sum(1 for item in report.results if item.needs_seal)
    tabs = st.tabs([
        "总览",
        f"缺项清单 {stats['missing']}",
        f"疑似待确认 {stats['suspect']}",
        f"已匹配 {stats['matched']}",
        f"印章核查 {seal_total}",
        f"未归类文件 {len(report.orphan_files)}",
        "标准预览",
    ])

    with tabs[0]:
        _render_overview(report)

    with tabs[1]:
        st.markdown("#### 缺项清单")
        st.caption("默认“暂不处理”；确认后请选择“确认缺失”，或改判“已提供 / 不适用”。")
        missing = report.by_state(STATE_MISSING)
        if not missing:
            st.success("本次没有缺失项。")
        else:
            keyword = st.text_input("搜索分类号 / 类目 / 文件", key="missing_search")
            codes = sorted({item.checkpoint.code[:3] for item in missing})
            code_filter = st.multiselect("按三级分类筛选", options=codes, key="missing_codes")
            results = _filtered_results(report, (STATE_MISSING,), keyword, code_filter)
            st.caption(f"共 {len(results)} 条")
            _render_editor(
                report, results, MISSING_CHOICES, "missing",
                st.session_state.override_store_path,
            )

    with tabs[2]:
        st.markdown("#### 疑似待确认")
        st.caption("有候选文件但证据不足。确认已提供后改判“已提供”，确认没有则改判“确认缺失”。")
        suspect = report.by_state(STATE_SUSPECT)
        if not suspect:
            st.success("本次没有疑似项。")
        else:
            keyword = st.text_input("搜索分类号 / 类目 / 文件 / 证据", key="suspect_search")
            codes = sorted({item.checkpoint.code[:3] for item in suspect})
            code_filter = st.multiselect("按三级分类筛选", options=codes, key="suspect_codes")
            results = _filtered_results(report, (STATE_SUSPECT,), keyword, code_filter)
            st.caption(f"共 {len(results)} 条")
            _render_editor(
                report, results, SUSPECT_CHOICES, "suspect",
                st.session_state.override_store_path,
            )

    with tabs[3]:
        st.markdown("#### 已匹配")
        matched = report.by_state(STATE_MATCHED)
        if not matched:
            st.info("暂无已匹配项。")
        else:
            keyword = st.text_input("搜索分类号 / 类目 / 文件 / 证据", key="matched_search")
            results = _filtered_results(report, (STATE_MATCHED,), keyword, [])
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "分类号": item.checkpoint.code,
                            "类目名称": item.checkpoint.category,
                            "归档内容": item.checkpoint.archive_content,
                            "主要项目文件": item.checkpoint.text,
                            "置信度": item.confidence,
                            "证据文件": item.evidence_text,
                            "正文": item.content_text if item.content_pages else "",
                            "印章": item.seal_text,
                            "说明": item.reason,
                        }
                        for item in results
                    ]
                ),
                hide_index=True,
                width="stretch",
                height=560,
            )

    with tabs[4]:
        st.markdown("#### 印章核查")
        st.caption(
            "对“报审表 / 验收记录 / 签证 / 合格证 / 竣工图”这类通常需要盖章的文种，"
            "在证据文件里做红色印章检测。只认红色印章：黑白扫描件判为“不可判”，需开卷确认。"
        )
        sealed = [item for item in report.results if item.needs_seal]
        if not sealed:
            st.info("本次没有识别到需要盖章的条目，或尚未读取文件正文。")
        else:
            counts = {"有": 0, "无": 0, "未知": 0}
            for item in sealed:
                counts[item.seal_state or "未知"] = counts.get(item.seal_state or "未知", 0) + 1
            _kpi_row(
                [
                    ("需盖章条目", len(sealed), "ink"),
                    ("检出印章", counts.get("有", 0), "matched"),
                    ("未见印章", counts.get("无", 0), "missing"),
                    ("不可判（黑白）", counts.get("未知", 0), "na"),
                ]
            )
            choice = st.radio(
                "筛选", ["全部", "未见印章", "不可判", "已检出"],
                horizontal=True, key="seal_filter", label_visibility="collapsed",
            )
            wanted = {"全部": None, "未见印章": "无", "不可判": "未知", "已检出": "有"}[choice]
            rows = [item for item in sealed if wanted is None or item.seal_state == wanted]
            st.caption(f"共 {len(rows)} 条")

            def _seal_note(item) -> str:
                """这一列只解释印章结论。

                原来直接放 item.reason（匹配理由），既重复了「证据文件」已有的路径，
                又会把单元格撑爆、右半句被裁掉。匹配理由在「已匹配 / 疑似待确认」页签里看。
                """
                if item.seal_state == "有":
                    pages = "、".join(f"第{p}页" for p in item.seal_pages[:3]) or "—"
                    return f"检出红色印章 {item.seal_count} 处（{pages}），只认红色"
                if item.seal_state == "无":
                    return "彩色页面上未检出红色印章，需开卷确认是否补盖"
                return "黑白扫描件，红色印章无法判定"
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "分类号": item.checkpoint.code,
                            "类目名称": item.checkpoint.category,
                            "主要项目文件": item.checkpoint.text,
                            "印章": item.seal_text,
                            "检出页码": "、".join(f"第{p}页" for p in item.seal_pages) or "—",
                            "证据文件": (
                                Path(item.evidence[0].rel_path).name if item.evidence else ""
                            ),
                            "说明": _seal_note(item),
                        }
                        for item in rows
                    ]
                ),
                hide_index=True,
                width="stretch",
                height=520,
                column_config={
                    "分类号": st.column_config.TextColumn(width=66),
                    "类目名称": st.column_config.TextColumn(width=84),
                    "主要项目文件": st.column_config.TextColumn(width=180),
                    "印章": st.column_config.TextColumn(width=104),
                    "检出页码": st.column_config.TextColumn(width=74),
                    "证据文件": st.column_config.TextColumn(width=210),
                    "说明": st.column_config.TextColumn(width=400),
                },
            )

    with tabs[5]:
        st.markdown("#### 未归类文件")
        st.caption(
            "未与任何检查点达到疑似阈值的文件，用于发现错放、模板外资料或命名问题。"
            + (
                "文件清单标准下，还包含“目录里有、清单里没有”的文件（说明列有标注）。"
                if template_set.meta.get("mode") == "inventory"
                else ""
            )
        )
        if not report.orphan_files:
            st.success("没有未归类文件。")
        else:
            keyword = st.text_input("搜索路径 / 候选检查点", key="orphan_search")
            rows = report.orphan_files
            if keyword.strip():
                rows = [
                    item for item in rows
                    if keyword in item["rel_path"] or keyword in item["best_cp_text"]
                ]
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "相对路径": item["rel_path"],
                            "大小": _fmt_size(item["size"]),
                            "最高分": f"{item['best_score']:.2f}",
                            "最佳候选检查点": item["best_cp_text"],
                            "最佳候选编号": item["best_cp_id"],
                            "说明": item.get("note", ""),
                        }
                        for item in rows
                    ]
                ),
                hide_index=True,
                width="stretch",
                height=560,
            )

    with tabs[6]:
        st.markdown("#### 标准预览")
        cps = template_set.checkpoints
        doc_type = st.selectbox(
            "资料类型", options=["全部", "8", "9"],
            format_func=lambda value: "全部" if value == "全部" else f"{value} {DOC_TYPES[value]}",
            key="preview_doc_type",
        )
        if doc_type != "全部":
            cps = [cp for cp in cps if cp.code.startswith(doc_type)]
        keyword = st.text_input("搜索分类号 / 类目 / 项目文件", key="preview_search")
        if keyword.strip():
            cps = [
                cp for cp in cps
                if keyword in cp.code or keyword in cp.category or keyword in cp.text
            ]
        visible = cps[:500]
        st.caption(
            f"共 {len(cps)} 条"
            + ("，仅显示前 500 条，可用搜索缩小范围" if len(cps) > 500 else "")
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "分类号": cp.code,
                        "类目名称": cp.category,
                        "归档内容": cp.archive_content,
                        "主要项目文件": cp.text,
                        "来源": cp.source,
                        "立卷": cp.filing,
                        "保管期限": cp.retention_text,
                        "不适用": "是" if cp.optional else "",
                        "层级": cp.display_path,
                    }
                    for cp in visible
                ]
            ),
            hide_index=True,
            width="stretch",
            height=560,
        )
        if st.button("生成 YAML 下载", key="build_yaml"):
            with st.spinner("正在生成 YAML…"):
                st.session_state.yaml_bytes = dump_checkpoint_set(template_set).encode("utf-8")
        if st.session_state.yaml_bytes:
            st.download_button(
                "⬇ 下载解析后的 YAML",
                data=st.session_state.yaml_bytes,
                file_name=f"检查点_{slugify(template_set.meta.get('template_name', 'template'))}.yaml",
                mime="text/yaml",
            )


# --------------------------------------------------------------------------
# 主区域
# --------------------------------------------------------------------------
def render_main(
    doc_type: str,
    root_text: str,
    unit: str,
    include_retention: bool,
    project_name_input: str,
    type_choice: tuple[str, str],
) -> None:
    template_set: CheckpointSet | None = st.session_state.template_set
    report_now: CheckReport | None = st.session_state.report
    chips = []
    if template_set:
        chips.append(f'<span class="chip">模版 <b>{len(template_set.checkpoints)} 条检查点</b></span>')
    if st.session_state.files:
        chips.append(f'<span class="chip">已扫描 <b>{len(st.session_state.files)} 个文件</b></span>')
    if report_now:
        chips.append(f'<span class="chip">责任单位 <b>{report_now.meta.get("unit", "")}</b></span>')
    st.markdown(
        '<div class="app-header"><div>'
        "<h1>竣工资料完整性检查器</h1>"
        '<p class="sub">对着归档范围模版逐条比对本地资料，判断已提供 / 疑似 / 缺失，'
        "并可核验 PDF 正文与红色印章。</p></div>"
        f'<div class="chip-row">{"".join(chips)}</div></div>',
        unsafe_allow_html=True,
    )

    if not template_set:
        st.markdown(
            """
            <div class="onboard">
              <div class="step-card"><div class="step-no">1</div>
                <h4>加载检查标准</h4>
                <p>上传《归档范围及保管期限表》docx / doc / YAML，或直接点左栏“加载内置模版”，
                   内置模版含 2907 条检查点、410 个类目。</p></div>
              <div class="step-card"><div class="step-no">2</div>
                <h4>选择资料目录</h4>
                <p>填本机竣工资料目录，选择资料类型与责任单位。目录只读，不会改动任何原始资料。</p></div>
              <div class="step-card"><div class="step-no">3</div>
                <h4>扫描并检查</h4>
                <p>先扫文件名与分类号做初筛；勾选正文核验后，疑似项会读 PDF 正文确认，
                   并识别需要盖章文种的红色印章。</p></div>
              <div class="step-card"><div class="step-no">4</div>
                <h4>核对并导出</h4>
                <p>在缺项清单 / 疑似待确认里逐条纠正，人工判断会按项目落盘；最后导出 8 个工作表的 Excel 整改单。</p></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    report: CheckReport | None = st.session_state.report
    if report:
        _render_report(report, template_set)
        return

    files = st.session_state.files
    _kpi_row(
        [
            ("模版检查点", len(template_set.checkpoints), "ink"),
            ("类目", len(template_set.nodes), "ink"),
            ("已扫描文件", len(files), "matched" if files else "ink"),
            ("当前单位", unit, "ink"),
        ]
    )
    if not files:
        st.markdown(
            '<div class="hint">请先在左栏选择资料目录，点击 <b>① 扫描目录</b>。</div>',
            unsafe_allow_html=True,
        )
    else:
        st.success(f"已扫描 {len(files)} 个文件，可以点击左栏“② 开始检查”。")
        st.caption(
            "检查范围会按“资料类型 + 项目类型 + 立卷单位”过滤；"
            "自动识别失败时可手动选择项目类型。"
        )

    with st.expander("当前设置"):
        st.write(
            {
                "资料类型": f"{doc_type} {DOC_TYPES.get(doc_type, '')}",
                "项目类型": type_choice[1],
                "责任单位": unit,
                "并入保管期限条目": include_retention,
                "资料目录": st.session_state.scan_root or root_text,
                "项目名称": project_name_input or "（使用目录名）",
            }
        )


def main() -> None:
    settings = render_sidebar()
    render_main(*settings)


if __name__ == "__main__":
    main()
