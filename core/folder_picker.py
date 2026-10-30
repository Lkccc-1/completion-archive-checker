"""弹系统原生的“选择文件夹”对话框。

Streamlit 的脚本跑在工作线程里，Tk 在工作线程里容易出怪问题（有的版本直接卡死），
所以另起一个**独立进程**跑对话框——在那个进程里它是主线程，选完把路径写到
stdout 拿回来。没装 Tk 或者对话框起不来时返回错误文字，界面照常能用手输兜底。
"""
from __future__ import annotations

import os
import subprocess
import sys

_DIALOG = r"""
import sys, traceback

def main():
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        initial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
        if initial and not __import__("os").path.isdir(initial):
            initial = None
        path = filedialog.askdirectory(
            title="选择资料目录（卷文件夹或整个工程目录）",
            initialdir=initial,
            mustexist=True,
            parent=root,
        )
        root.destroy()
        sys.stdout.buffer.write(("OK:" + (path or "")).encode("utf-8"))
    except Exception:
        sys.stdout.buffer.write(
            ("ERR:" + traceback.format_exc().strip().splitlines()[-1]).encode("utf-8")
        )

main()
"""


def parse_output(raw: bytes) -> tuple[str, str]:
    """子进程输出 → (路径, 错误)。用户取消时两者都为空。"""
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("OK:"):
        return text[3:].strip(), ""
    if text.startswith("ERR:"):
        return "", text[4:].strip() or "对话框打不开"
    if not text:
        return "", ""
    return "", text[:200]


def pick_folder(initial: str = "", timeout: float = 900) -> tuple[str, str]:
    """弹出系统的选择文件夹对话框。返回 (选中的路径, 错误)；取消返回 ("", "")。"""
    try:
        import tkinter  # noqa: F401  确认这台机器有图形库
    except Exception as exc:  # noqa: BLE001
        return "", f"这台机器没有 Tk 图形库：{exc}"

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：别闪黑框
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _DIALOG, initial or ""],
            capture_output=True,
            timeout=timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return "", "选文件夹窗口超过 15 分钟没有操作，已取消。"
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"

    path, error = parse_output(proc.stdout)
    if path or error:
        return path, error
    detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
    return "", detail[-1] if detail else "对话框没有返回结果"
