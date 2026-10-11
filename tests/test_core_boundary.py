from __future__ import annotations

import ast
import unittest
from pathlib import Path


class CoreBoundaryTests(unittest.TestCase):
    def test_core_does_not_import_astrbot(self) -> None:
        root = Path(__file__).resolve().parents[1] / "alice_agent"
        violations: list[str] = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name == "astrbot" or name.startswith("astrbot.") for name in names):
                    violations.append(str(path.relative_to(root)))
        self.assertEqual(violations, [])

    def test_core_does_not_import_jev_infrastructure(self) -> None:
        root = Path(__file__).resolve().parents[1] / "alice_agent"
        forbidden_roots = {"httpx", "typesafe", "typesafe_sdk"}
        violations: list[str] = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name.split(".", 1)[0] in forbidden_roots for name in names):
                    violations.append(str(path.relative_to(root)))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
