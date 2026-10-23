"""Evaluate saved decisions against reviewed JSONL labels; never calls a model."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_alice_agent.alice_agent.decision.evaluation import evaluate_predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-proposed", action="store_true", help="Format checks only; proposed labels are not reviewed evidence")
    args = parser.parse_args()
    def read(path):
        with path.open(encoding="utf-8-sig") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    report = evaluate_predictions(read(args.labels), read(args.predictions), include_proposed=args.include_proposed)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
