from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from astrbot_plugin_alice_agent.scripts.verify_release import build_package, source_files, verify_package, verify_source


class ReleasePackageTests(unittest.TestCase):
    def test_source_archive_preserves_static_knowledge_and_excludes_runtime_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "metadata.yaml").write_text("version: 1.2.3\n", encoding="utf-8")
            static = root / "alice_agent/knowledge/data/facts.jsonl"
            static.parent.mkdir(parents=True)
            static.write_text('{"id":"curated"}\n', encoding="utf-8")
            for name in ("data/alice.db", "adapters/cache.db", "adapters/user_config.json", ".env", "plugin_data/private.json", "alice_agent/__pycache__/cache.pyc"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private", encoding="utf-8")
            names = {path.relative_to(root).as_posix() for path in source_files(root)}
            self.assertEqual(names, {"metadata.yaml", "alice_agent/knowledge/data/facts.jsonl"})
            target = root / "package.zip"
            report = build_package(root, target)
            self.assertEqual(report["files"], 2)
            self.assertEqual(report, verify_package(target))
            # A second build of the same files has the same bytes and hash.
            self.assertEqual(report["sha256"], build_package(root, root / "second.zip")["sha256"])

    def test_archive_tampering_and_unsafe_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "metadata.yaml").write_text("version: 1.2.3\n", encoding="utf-8")
            target = root / "good.zip"
            build_package(root, target)
            with zipfile.ZipFile(target) as archive:
                entries = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
            with zipfile.ZipFile(root / "changed.zip", "w") as archive:
                for name, data in entries:
                    archive.writestr(name, b"version: 9.9.9" if name.endswith("metadata.yaml") else data)
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_package(root / "changed.zip")
            with zipfile.ZipFile(root / "unsafe.zip", "w") as archive:
                archive.writestr("astrbot_plugin_alice_agent/../../escape.py", b"pass")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                verify_package(root / "unsafe.zip")

    def test_current_source_has_expected_schema_and_core_boundary(self):
        report = verify_source(Path(__file__).resolve().parents[1], check_composition=False)
        self.assertGreater(report["python_files"], 170)
