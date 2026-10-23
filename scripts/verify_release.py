"""Audit source/config boundaries and build/verify a deterministic source ZIP."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.util
import json
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


PACKAGE = "astrbot_plugin_alice_agent"
ROOT_FILES = {"__init__.py", "main.py", "metadata.yaml", "requirements.txt", "_conf_schema.json",
              "README.md", "HANDOFF.md", "SERVER_UPDATE.md", "CHANGELOG.md", "LICENSE"}
SOURCE_DIRS = {"adapters", "alice_agent", "docs", "scripts", "tests", ".github"}
EXTENSIONS = {".py", ".md", ".json", ".jsonl", ".txt", ".yml", ".yaml"}
MANIFEST = "_source_manifest.json"


def source_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or path.is_symlink() or any((root.joinpath(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts))):
            continue
        static_knowledge = relative.parts[:3] == ("alice_agent", "knowledge", "data")
        if any(part in {"__pycache__", ".venv", "plugin_data", "test_turns", "node_modules"} for part in relative.parts) or ("data" in relative.parts and not static_knowledge):
            continue
        if path.name.endswith("_config.json") or path.name.startswith(".env"):
            continue
        if len(relative.parts) == 1:
            allowed = path.name in ROOT_FILES
        else:
            allowed = relative.parts[0] in SOURCE_DIRS and path.suffix in EXTENSIONS and not path.name.startswith(".")
        if allowed:
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def version_of(metadata: str) -> str:
    match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", metadata, re.MULTILINE)
    if match is None:
        raise ValueError("metadata has no strict release version")
    return match[1]


def verify_source(root: Path, *, check_composition: bool = True) -> dict:
    files = source_files(root)
    names = {path.relative_to(root).as_posix() for path in files}
    required = {"main.py", "metadata.yaml", "_conf_schema.json", "alice_agent/core/pipeline.py",
                "adapters/astrbot/reactive.py", "adapters/astrbot/reply_delivery.py", "tests/test_delivery_recovery.py",
                "alice_agent/knowledge/data/facts.jsonl", "alice_agent/knowledge/data/dialogues.jsonl", "alice_agent/knowledge/data/source_manifest.json"}
    if not required <= names:
        raise ValueError(f"missing required source files: {sorted(required - names)}")
    version = version_of((root / "metadata.yaml").read_text(encoding="utf-8"))
    if f"v{version}" not in (root / "README.md").read_text(encoding="utf-8").splitlines()[0]:
        raise ValueError("README heading and metadata versions disagree")
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    types = {"bool": bool, "int": int, "float": (int, float), "string": str, "text": str}
    for name, spec in schema.items():
        if not isinstance(spec, dict) or spec.get("type") not in types or "default" not in spec:
            raise ValueError(f"invalid config specification: {name}")
        default = spec["default"]
        if not isinstance(default, types[spec["type"]]) or (spec["type"] in {"int", "float"} and isinstance(default, bool)):
            raise ValueError(f"invalid config default type: {name}")
    compiled = 0
    for path in files:
        if path.suffix != ".py":
            continue
        compile(path.read_bytes(), str(path), "exec")
        compiled += 1
        if path.relative_to(root).parts[0] != "alice_agent":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names_imported = []
            if isinstance(node, ast.Import):
                names_imported = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names_imported = [node.module or ""]
            if any(name.split(".")[0] in {"astrbot", "httpx", "typesafe", "typesafe_sdk"} for name in names_imported):
                raise ValueError(f"Core imports infrastructure: {path.relative_to(root)}")
    if check_composition:
        # Also works when the checkout directory is named "plugin" in CI.
        spec = importlib.util.spec_from_file_location(PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
        main = importlib.import_module(PACKAGE + ".main")
        config_type = importlib.import_module(PACKAGE + ".alice_agent.config").AgentConfig
        raw = {key: value["default"] for key, value in schema.items()}
        built, defaults = main._build_config(raw), config_type()
        # The public persona template and concrete Jev endpoint are composition
        # defaults; the independent Core defaults intentionally have neither.
        differences = {key for key in defaults.__dataclass_fields__ if getattr(built, key) != getattr(defaults, key)}
        if differences - {"persona_profile", "jev_endpoint"}:
            raise ValueError(f"schema/composition/Core defaults drift: {sorted(differences)}")
        media = main._build_media_config(raw)
        if not media.text_first or media.render_timeout_seconds != 12 or built.jev_affect_intervention_enabled:
            raise ValueError("reliability defaults changed unexpectedly")
    return {"version": version, "files": len(files), "python_files": compiled, "schema_fields": len(schema)}


def build_package(root: Path, target: Path) -> dict:
    files = source_files(root)
    manifest = {"schema_version": 1, "version": version_of((root / "metadata.yaml").read_text(encoding="utf-8")),
                "files": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(PACKAGE + "/" + path.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo(PACKAGE + "/" + MANIFEST, (1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode())
    return verify_package(target)


def verify_package(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or sum(item.file_size for item in archive.infolist()) > 80 * 1024 * 1024:
            raise ValueError("duplicate or oversized archive members")
        for item in archive.infolist():
            parts = PurePosixPath(item.filename).parts
            if not parts or parts[0] != PACKAGE or ".." in parts or "\\" in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("unsafe archive path")
        manifest = json.loads(archive.read(PACKAGE + "/" + MANIFEST))
        expected = {PACKAGE + "/" + name for name in manifest["files"]} | {PACKAGE + "/" + MANIFEST}
        if set(names) != expected:
            raise ValueError("archive and manifest file sets disagree")
        for name, digest in manifest["files"].items():
            data = archive.read(PACKAGE + "/" + name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("archive content hash mismatch: " + name)
            if name.endswith(".py"):
                compile(data, name, "exec")
            member = PurePosixPath(name)
            if (len(member.parts) == 1 and name not in ROOT_FILES) or (len(member.parts) > 1 and member.parts[0] not in SOURCE_DIRS):
                raise ValueError("unexpected archive content")
            if name.endswith("_config.json") or member.suffix not in EXTENSIONS:
                raise ValueError("runtime data in source archive")
        version = version_of(archive.read(PACKAGE + "/metadata.yaml").decode())
        if version != manifest["version"]:
            raise ValueError("archive version mismatch")
    return {"version": version, "files": len(manifest["files"]), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--build", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--run-tests", action="store_true", help="run the full suite from a fresh unpacked source ZIP")
    args = parser.parse_args()
    result = {"source": verify_source(args.root.resolve())}
    if args.build:
        result["package"] = build_package(args.root.resolve(), args.build.resolve())
    if args.package:
        result["package"] = verify_package(args.package.resolve())
    if args.run_tests:
        package = args.build or args.package
        if package is None:
            parser.error("--run-tests requires --build or --package")
        with tempfile.TemporaryDirectory(prefix="alice-release-test-") as folder:
            with zipfile.ZipFile(package.resolve()) as archive:
                archive.extractall(folder)
            outcome = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", PACKAGE + "/tests", "-t", ".", "-q"], cwd=folder)
            if outcome.returncode:
                raise SystemExit(outcome.returncode)
        result["unpacked_suite"] = "passed"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
