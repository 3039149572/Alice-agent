from __future__ import annotations

import math
from collections import defaultdict


def evaluate_predictions(labels: list[dict], predictions: list[dict], *, include_proposed: bool = False) -> dict:
    """Offline evaluation only. Inputs/text never appear in the returned report."""
    selected = [row for row in labels if row.get("reviewed") is True or include_proposed]
    by_id = {}
    for row in predictions:
        identity = row.get("id")
        if not isinstance(identity, str) or identity in by_id:
            raise ValueError("Prediction IDs must be unique strings")
        by_id[identity] = row
    groups = defaultdict(list)
    seen = set()
    for row in selected:
        identity, task = row.get("id"), row.get("task")
        if not isinstance(identity, str) or identity in seen or not isinstance(task, str) or not isinstance(row.get("expected"), dict) or not row["expected"]:
            raise ValueError("Label IDs, task and expected fields must be valid and unique")
        seen.add(identity)
        prediction = by_id.get(identity)
        if prediction is not None and prediction.get("task") != task:
            raise ValueError("Prediction task differs from label task")
        groups[task].append((row, prediction))
    report = {"selected_samples": len(selected), "reviewed_only": not include_proposed,
              "quality_claim_supported": False, "tasks": {}}
    for task, pairs in groups.items():
        evaluated = [(label, pred) for label, pred in pairs if pred is not None]
        correct, fields, latencies, calibration, fallback = 0, defaultdict(lambda: [0, 0]), [], [], 0
        strata = defaultdict(lambda: [0, 0])
        for label, prediction in evaluated:
            answers = prediction.get("answers", {})
            if not isinstance(answers, dict):
                answers = {}
            exact = all(answers.get(key) == value for key, value in label["expected"].items())
            correct += int(exact)
            stratum = str(label.get("stratum", "unspecified"))
            strata[stratum][0] += int(exact)
            strata[stratum][1] += 1
            for key, value in label["expected"].items():
                fields[key][0] += int(answers.get(key) == value)
                fields[key][1] += 1
            confidence = prediction.get("confidence")
            if type(confidence) in (float, int) and math.isfinite(confidence) and 0 <= confidence <= 1:
                calibration.append((float(confidence), int(exact)))
            latency = prediction.get("latency_ms")
            if type(latency) in (float, int) and math.isfinite(latency) and latency >= 0:
                latencies.append(float(latency))
            fallback += int(prediction.get("fallback") is True)
        bins, ece = [], 0.0
        for low, high in ((0, .5), (.5, .7), (.7, .8), (.8, .9), (.9, 1.000001)):
            values = [(confidence, outcome) for confidence, outcome in calibration if low <= confidence < high]
            if values:
                mean = sum(value[0] for value in values) / len(values)
                accuracy = sum(value[1] for value in values) / len(values)
                ece += abs(mean - accuracy) * len(values) / len(calibration)
                bins.append({"low": low, "high": min(1, high), "count": len(values),
                             "mean_confidence": mean, "accuracy": accuracy})
        latencies.sort()
        report["tasks"][task] = {
            "selected": len(pairs), "evaluated": len(evaluated), "missing": len(pairs) - len(evaluated),
            "coverage": len(evaluated) / len(pairs), "exact_match_rate_all_selected": correct / len(pairs),
            "exact_match_accuracy_evaluated": correct / len(evaluated) if evaluated else None,
            "field_accuracy": {key: value[0] / value[1] for key, value in fields.items()},
            "strata": {key: {"correct": value[0], "evaluated": value[1]} for key, value in strata.items()},
            "fallback_rate": fallback / len(evaluated) if evaluated else None,
            "latency_p95_ms": latencies[max(0, math.ceil(len(latencies) * .95) - 1)] if latencies else None,
            "calibration_ece": ece if calibration else None, "calibration_bins": bins,
        }
    report["reviewed_coverage_complete"] = bool(selected) and not include_proposed and all(
        value["missing"] == 0 for value in report["tasks"].values()
    )
    return report
