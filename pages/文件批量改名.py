"""文件批量改名：按正文内容给文件起名，确认后直接改本地文件。

单独一页，避免和“完整性检查”那边的状态搅在一起。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.folder_picker import pick_folder  # noqa: E402
from core.renamer import (  # noqa: E402
    MAX_NAME,
    RenameItem,
    apply_plan,
    iter_files,
    log_path,
    plan_folder,
    undo_last,
)

st.set_page_config(
    page_title="文件批量改名",
    page_icon="🏷️",
    layout="wide",
    initial_sidebar_state="expanded",
)

SAMPLE_DIR = BASE_DIR / "文件改名测试集"
CACHE = BASE_DIR / "data" / "rename_logs" / "rename_content_cache.json"


def _default_folder() -> str:
    saved = st.session_state.get("rename_root")
    if saved:
        return saved
    return str(SAMPLE_DIR) if SAMPLE_DIR.exists() else ""



# --------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 1 选择文件夹")
    if "rename_root" not in st.session_state:
        st.session_state["rename_root"] = _default_folder()
    if st.button("📁 选择文件夹", width="stretch"):
        with st.spinner("已弹出系统的选择窗口，选完这里会自动填上…"):
            picked, pick_error = pick_folder(st.session_state.get("rename_root", ""))
        if pick_error:
            st.session_state.rename_pick_error = pick_error
        elif picked:
            st.session_state.rename_root = picked
            st.session_state.rename_pick_error = ""
            st.session_state.rename_plan = None
            st.session_state.rename_result = None
    if st.session_state.get("rename_pick_error"):
        st.error(st.session_state.rename_pick_error)

    folder = str(st.session_state.get("rename_root") or "").strip()
    if folder:
        st.caption(f"已选目录：\n\n`{folder}`")
    else:
        st.caption("还没有选目录，点上面的“选择文件夹”。")
    with st.expander("对话框打不开时：手动粘贴路径"):
        manual = st.text_input("路径", key="rename_root_manual", label_visibility="collapsed")
        if st.button("用这个路径", key="rename_use_manual", width="stretch") and manual.strip():
            st.session_state.rename_root = manual.strip()
            st.session_state.rename_pick_error = ""
    project_name = st.text_input(
        "工程全名（可选）",
        key="rename_project",
        help="填了就按这个字符串从标题里删掉；不填会在正文里自动找重复出现的“××工程”。",
    ).strip()
    max_pages = st.slider("每份最多读几页", 1, 5, 2, key="rename_pages")
    use_ocr = st.checkbox(
        "扫描件走 OCR", value=True, key="rename_ocr",
        help="关掉更快，但纯扫描件读不出字，会保留原名。",
    )
    scan = st.button("🪄 生成新名字", type="primary", width="stretch")
    # 撤销放在侧栏、不依赖预览表：改完名后预览表会清空，按钮必须还在
    undo = st.button("↩️ 撤销上一次改名", width="stretch")

st.title("🏷️ 文件批量改名")
st.caption(
    f"读文件正文 → 取文件自带的标题 → 删掉工程全名、单位名、文号 → 生成不超过 {MAX_NAME} 字的名字。"
    "优先用文件里写着的标题，不自己另起一个；读到能定名就停止翻页。"
)

if scan:
    root = Path(folder)
    if not folder or not root.is_dir():
        st.error("文件夹不存在，请重新填写路径。")
    else:
        files = iter_files(root)
        if not files:
            st.warning("这个文件夹里没有支持的文件（PDF / Word / 图片）。")
        else:
            progress = st.progress(0.0, text=f"共 {len(files)} 份，正在读正文…")
            items = plan_folder(
                root,
                project_name=project_name,
                max_pages=max_pages,
                use_ocr=use_ocr,
                cache_path=CACHE,
                progress=lambda done, total: progress.progress(
                    done / total, text=f"{done}/{total} 份 · 正在读正文…"
                ),
            )
            progress.empty()
            st.session_state.rename_plan = items
            st.session_state.rename_root_dir = str(root)
            st.session_state.rename_result = None

if undo:
    target = Path(folder) if folder else None
    if target is None or not target.is_dir():
        st.error("先填一个存在的文件夹，再撤销。")
    else:
        st.session_state.rename_result = undo_last(
            log_path(target, base=BASE_DIR / "data")
        )
        st.session_state.rename_plan = None

items: list[RenameItem] | None = st.session_state.get("rename_plan")
if items:
    root_dir = Path(st.session_state.get("rename_root_dir") or folder)
    changed = [item for item in items if item.changed]
    failed = [item for item in items if item.error or not item.new_name]

    cols = st.columns(4)
    cols[0].metric("文件总数", len(items))
    cols[1].metric("会改名", len(changed))
    cols[2].metric("保持原名", len(items) - len(changed))
    cols[3].metric("读不了", len(failed))

    st.markdown("#### 2 看一眼新名字（可以直接改表格）")
    frame = pd.DataFrame(
        [
            {
                "原名": item.old_name,
                "新文件名": (item.new_name or item.path.stem) + item.ext,
                "来源": item.source or "—",
                "文件里认到的标题": (item.title or "—")[:60],
                "说明": item.note or item.error or "",
            }
            for item in items
        ]
    )
    edited = st.data_editor(
        frame,
        key="rename_editor",
        hide_index=True,
        width="stretch",
        height=min(620, 38 * (len(frame) + 1)),
        disabled=["原名", "来源", "文件里认到的标题", "说明"],
        column_config={
            "原名": st.column_config.TextColumn(width="medium"),
            "新文件名": st.column_config.TextColumn(
                "新文件名（可改）", width="large",
                help="含扩展名。改完点下面的按钮才真正落盘。",
            ),
        },
    )

    st.markdown("#### 3 确认改名")
    st.caption(
        f"只改文件名，不动内容；同名冲突会跳过后单独列出来。"
        f"每次改名都会写日志（`{log_path(root_dir, base=BASE_DIR / 'data').relative_to(BASE_DIR)}`），"
        "改错了可以一键撤销上一次。"
    )
    confirm = st.button(
        f"✅ 确认改名（{len(changed)} 份）", type="primary", width="stretch"
    )

    if confirm:
        plan = []
        for item, row in zip(items, edited.to_dict("records")):
            raw = str(row.get("新文件名") or "").strip()
            stem = Path(raw).stem if Path(raw).suffix else raw
            item.new_name = stem[:MAX_NAME].strip(" .")
            plan.append(item)
        result = apply_plan(plan, log_file=log_path(root_dir, base=BASE_DIR / "data"))
        st.session_state.rename_result = result
        st.session_state.rename_plan = None

result = st.session_state.get("rename_result")
if result:
    done = result.get("done") or []
    bad = result.get("failed") or []
    if done:
        st.success(f"完成 {len(done)} 份改名。")
        st.dataframe(
            pd.DataFrame([{"原名": d.get("old_name", d.get("old")), "新名": d.get("new_name", d.get("new"))} for d in done]),
            hide_index=True,
            width="stretch",
        )
    if bad:
        st.warning(f"{len(bad)} 份没改：")
        st.dataframe(pd.DataFrame(bad), hide_index=True, width="stretch")
    if not done and not bad:
        st.info(result.get("reason") or "没有需要改的文件。")
