"""Choose the cheapest qualified measured profile; never infer missing measurements.

Input is a private JSON array of candidates. Each candidate has id, benchmark,
scores, hosted, and cost objects from the qualification/forecast tools. An
unavailable candidate can instead provide an explicit unavailable_reason.
"""

import argparse
import json
import math
from pathlib import Path

from check_qualification import evaluate
from voice_studio_engine.models import model_lock


def select_profile(candidates):
    assessed = []
    seen = set()
    for candidate in candidates:
        identifier = candidate["id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("Each candidate requires a unique nonempty id")
        seen.add(identifier)
        benchmark = candidate.get("benchmark", {})
        model, device = benchmark.get("model"), benchmark.get("device")
        failures = []
        if candidate.get("unavailable_reason"):
            failures.append(str(candidate["unavailable_reason"]))
        else:
            failures.extend(evaluate(candidate.get("scores", []), candidate.get("hosted")))
            if model not in {"original", "turbo", "nano"} or device not in {"cpu", "cuda"}:
                failures.append("Provide a measured hosted CPU or CUDA model profile")
            if benchmark.get("pins") != model_lock():
                failures.append("Benchmark pins must match the current immutable model lock")
            if benchmark.get("cpu_threads") == 8 and candidate.get("eight_cpu_quota_verified") is not True:
                failures.append("Eight CPUs require verified account quota")
        cost = candidate.get("cost", {}).get("cost_per_completed_message_usd")
        if isinstance(cost, bool) or not isinstance(cost, (float, int)) or not math.isfinite(cost) or cost < 0:
            failures.append("Provide a measured complete cost per completed message")
            cost = None
        scores = candidate.get("scores", [])
        cpu_measured = bool(scores) and all(
            all(isinstance(row.get(field), (float, int)) and not isinstance(row.get(field), bool)
                and 1 <= row[field] <= 5 for field in ("naturalness", "similarity"))
            and all(type(row.get(field)) is bool for field in (
                "critical_correct", "complete", "owner_approved", "unwanted_repetition"))
            for row in scores
        ) and bool((candidate.get("hosted") or {}).get("ready_to_artifacts_seconds"))
        assessed.append({"id": identifier, "model": model, "device": device,
                         "cpu_measurements_reviewed": cpu_measured,
                         "cost_per_completed_message_usd": cost, "failures": failures})
    # GPU comparison is eligible only after measured CPU candidates fail.
    cpu = [row for row in assessed if row["device"] == "cpu"]
    cpu_passed = any(not row["failures"] for row in cpu)
    for row in assessed:
        if row["device"] == "cuda" and (not cpu or cpu_passed or not all(c["cpu_measurements_reviewed"] for c in cpu)):
            row["failures"].append("Use CPU when it qualifies; measure CPU before comparing GPU")
    eligible = [row for row in assessed if not row["failures"]]
    selected = min(eligible, key=lambda row: (row["cost_per_completed_message_usd"], row["id"])) if eligible else None
    return {"selected": selected["id"] if selected else None, "qualified": selected is not None,
            "candidates": assessed, "stable_release_approved": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", type=Path)
    args = parser.parse_args()
    try:
        report = select_profile(json.loads(args.candidates.read_text()))
    except (ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Invalid candidate evidence: {exc}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    raise SystemExit(0 if report["qualified"] else 1)


if __name__ == "__main__":
    main()
