from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import BASE, LEGACY_YAML

import yaml

from core.models import CheckpointSet
from core.overrides import (
    OverrideStore,
    load_config,
    project_dir,
    save_config,
    slugify,
)
from core.template_loader import load_template
from core.yaml_loader import dump_checkpoint_set, parse_yaml


class LegacyYamlTests(unittest.TestCase):
    def test_legacy_counts(self):
        cs = load_template(LEGACY_YAML)
        self.assertEqual(len(cs.checkpoints), 21)
        self.assertEqual(len(cs.nodes), 5)
        self.assertTrue(cs.warnings)

    def test_legacy_project_type(self):
        cs = load_template(LEGACY_YAML)
        types = cs.project_types()
        self.assertTrue(types)
        self.assertEqual(types[0].code, "81")


class YAMLRoundTripTests(unittest.TestCase):
    @unittest.skipUnless(
        (BASE / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx").exists(),
        "归档范围模版未随仓库分发（内部资料）",
    )
    def test_dump_and_reload(self):
        full = load_template(BASE / "电网建设项目、设备仪器类分类、归档范围及保管期限表.docx")
        small = CheckpointSet(
            meta=full.meta,
            nodes=full.nodes,
            checkpoints=full.checkpoints[:8],
            warnings=["roundtrip"],
        )
        text = dump_checkpoint_set(small)
        data = yaml.safe_load(text)
        self.assertEqual(len(data["checkpoints"]), 8)
        self.assertEqual(data["checkpoints"][0]["match"]["filename_exclude"], [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roundtrip.yaml"
            path.write_text(text, encoding="utf-8")
            loaded = parse_yaml(path)
        self.assertEqual(len(loaded.checkpoints), 8)
        self.assertEqual(loaded.checkpoints[0].text, full.checkpoints[0].text)
        self.assertEqual(
            loaded.checkpoints[0].retention, full.checkpoints[0].retention
        )


class OverrideStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "data" / "overrides.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_load_clear(self):
        store = OverrideStore(self.path)
        self.assertEqual(store.load(), {})
        store.set("CP-1", "已匹配", note="现场已提供", reviewer="张三")
        loaded = store.load()
        self.assertEqual(loaded["CP-1"]["state"], "已匹配")
        self.assertEqual(loaded["CP-1"]["note"], "现场已提供")
        self.assertEqual(store.load_meta()["updated_at"][:2], "20")
        store.clear()
        self.assertEqual(store.load(), {})

    def test_save_preserves_other_items(self):
        store = OverrideStore(self.path)
        store.save({"CP-1": {"state": "缺失"}, "CP-2": {"state": "已匹配"}})
        items = store.load()
        items["CP-3"] = {"state": "不适用"}
        store.save(items)
        self.assertEqual(set(store.load()), {"CP-1", "CP-2", "CP-3"})

    def test_corrupt_file_returns_empty(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(OverrideStore(self.path).load(), {})

    def test_csv(self):
        store = OverrideStore(self.path)
        store.set("CP-1", "已匹配", note="备注", reviewer="张三")
        csv_text = store.to_csv()
        self.assertIn("检查点编号", csv_text)
        self.assertIn("CP-1", csv_text)

    def test_project_dir_is_stable(self):
        a = project_dir(self.root, self.root)
        b = project_dir(self.root, self.root)
        self.assertEqual(a, b)
        self.assertTrue(a.name.startswith(slugify(self.root.name)))

    def test_config_roundtrip(self):
        path = self.root / "config.json"
        save_config(path, {"unit": "施工", "project": "测试"})
        self.assertEqual(load_config(path)["unit"], "施工")
        self.assertEqual(load_config(self.root / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
