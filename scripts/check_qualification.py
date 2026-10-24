"""Fail closed: benchmark timings alone never approve a release."""

import argparse
import json
import math
from pathlib import Path


def evaluate(scores, hosted=None):
    failures = []
    ids = {row["script_id"] for row in scores}
    if len(ids) < 20 or any(
        len({r["seed"] for r in scores if r["script_id"] == sid}) < 3 for sid in ids
    ):
        failures.append("Require twenty scripts and at least three seeds per script")
    for field in ["naturalness", "similarity"]:
        values = [row.get(field) for row in scores]
        if not values or any(
            not isinstance(v, (int, float)) or isinstance(v, bool) or not 1 <= v <= 5
            for v in values
        ):
            failures.append(f"Complete all {field} scores on a one-to-five scale")
        elif sum(values) / len(values) < 4:
            failures.append(f"Mean {field} must be at least four")
    for field, expected in [
        ("critical_correct", True),
        ("complete", True),
        ("owner_approved", True),
        ("unwanted_repetition", False),
    ]:
        if any(row.get(field) is not expected for row in scores):
            failures.append(f"Every take must pass {field}")
    if any(not 30 <= row.get("output_seconds", 0) <= 60 for row in scores):
        failures.append(
            "Agree and qualify a message set whose generated durations are 30–60 seconds"
        )
    if hosted is None:
        failures.append(
            "Hosted end-to-end measurements and lifecycle recovery evidence are missing"
        )
    else:
        samples = hosted.get("ready_to_artifacts_seconds", [])
        if (
            len(samples) < len(scores)
            or not samples
            or any(
                not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in samples
            )
        ):
            failures.append(
                "Provide complete warm hosted measurements including durable artifact storage"
            )
        elif sorted(samples)[math.ceil(0.95 * len(samples)) - 1] > 30:
            failures.append("Hosted warm p95 exceeds thirty seconds")
        for key in [
            "recovery_passed",
            "start_generate_download_stop_passed",
            "cost_accounting_complete",
            "owner_budget_approved",
        ]:
            if hosted.get(key) is not True:
                failures.append(f"Missing {key}")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scores", type=Path)
    parser.add_argument("--hosted", type=Path)
    args = parser.parse_args()
    failures = evaluate(
        json.loads(args.scores.read_text()),
        json.loads(args.hosted.read_text()) if args.hosted else None,
    )
    print(json.dumps({"qualified": not failures, "failures": failures}, indent=2))
    raise SystemExit(bool(failures))
