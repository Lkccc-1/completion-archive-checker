"""人工纠正落盘 + 项目配置目录。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

def slugify(value: str, limit: int = 60) -> str:
    value = re.sub(r"[^\w一-鿿.-]+", "_", value or "").strip("._-")
    return (value or "project")[:limit]


def project_dir(base_dir: str | Path, root: str | Path) -> Path:
    root_text = str(Path(root).resolve())
    digest = hashlib.sha1(root_text.encode("utf-8")).hexdigest()[:8]
    return Path(base_dir) / "data" / "projects" / f"{slugify(Path(root_text).name)}-{digest}"


class OverrideStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, dict):
            return {str(key): value for key, value in items.items() if isinstance(value, dict)}
        if isinstance(data, dict):
            return {str(key): value for key, value in data.items() if isinstance(value, dict)}
        return {}

    def load_meta(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data.get("meta", {}) if isinstance(data, dict) else {}

    def save(self, items: dict[str, dict[str, Any]], meta: dict[str, Any] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                **(meta or {}),
            },
            "items": items,
        }
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp"
        )
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            handle.close()
            Path(handle.name).replace(self.path)
        finally:
            if not handle.closed:
                handle.close()
            temp_path = Path(handle.name)
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def set(
        self,
        cp_id: str,
        state: str,
        note: str = "",
        reviewer: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        items = self.load()
        items[cp_id] = {
            "state": state,
            "note": note,
            "reviewer": reviewer,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        self.save(items, meta)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def to_csv(self, checkpoints: dict[str, Any] | None = None) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["检查点编号", "分类号", "主要项目文件", "纠正状态", "备注", "纠正人", "时间"])
        for cp_id, item in sorted(self.load().items()):
            cp = (checkpoints or {}).get(cp_id)
            writer.writerow(
                [
                    cp_id,
                    getattr(cp, "code", "") if cp else "",
                    getattr(cp, "text", "") if cp else "",
                    item.get("state", ""),
                    item.get("note", ""),
                    item.get("reviewer", ""),
                    item.get("time", ""),
                ]
            )
        return buffer.getvalue()


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
