"""Read-only verification of curated data against the supplied archive and TXT."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def verify(directory: Path, archive: Path, novel: Path | None = None) -> dict:
    manifest = json.loads((directory / "source_manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    lines = None
    if novel is not None:
        raw = novel.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == manifest["original_novel_sha256"]
        text = raw.decode("utf-8-sig")
        lines = text.splitlines()
    quote_checks = evidence_checks = 0
    counts = {}
    with zipfile.ZipFile(archive) as zipped:
        for member, filename in (("03_人物对话示例.jsonl", "dialogues.jsonl"),
                                 ("04_剧情与关系资料.jsonl", "facts.jsonl")):
            blob = zipped.read("alice_context_pack_full/" + member)
            assert hashlib.sha256(blob).hexdigest() == manifest["source_member_sha256"][member]
            original = {c["id"]: c for row in blob.decode("utf-8-sig").splitlines() if row.strip()
                        for c in [json.loads(row)]}
            cards = [json.loads(row) for row in (directory / filename).read_text(encoding="utf-8").splitlines() if row.strip()]
            assert len(cards) == len({c["id"] for c in cards}) == manifest["counts"][filename]["included"]
            counts[filename] = len(cards)
            for card in cards:
                source = original[card["id"]]
                assert card["source"]["sha256"] == manifest["original_novel_sha256"]
                assert card["source_family"] == "main"
                assert card["summary"] == source["summary"]
                if filename == "dialogues.jsonl":
                    assert card["persona_origin"] == "knight" and source["default_retrieval"] is True
                    assert card["quote"] == source["speech"]["quote"]
                    assert card["match_terms"]
                    if lines is not None:
                        ref = source["source"]
                        assert text[ref["char_start"]:ref["char_end"]] == card["quote"], card["id"]
                    quote_checks += 1
                elif card["persona_origin"] == "childhood":
                    assert card["knowledge_mode"] == "reader_background"
                for evidence in source["evidence"]:
                    if lines is not None:
                        assert evidence["quote"] in lines[evidence["line"] - 1], (card["id"], evidence["line"])
                    evidence_checks += 1
    return {"passed": True, "counts": counts, "dialogue_quote_checks": quote_checks,
            "source_evidence_checks": evidence_checks, "original_txt_checked": novel is not None,
            "source_sha256": manifest["original_novel_sha256"],
            "scope": "source integrity and literal quotations, not a model-quality or exhaustive semantic audit"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--novel", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.directory, args.archive, args.novel)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
