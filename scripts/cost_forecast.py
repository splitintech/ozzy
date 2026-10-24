"""Calculate a forecast from measured durations and explicit provider rates."""

import argparse
import json
import math
from pathlib import Path


def forecast(data):
    def number(key):
        value = data.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"Provide a finite nonnegative {key}")
        return float(value)

    messages = number("monthly_completed_messages")
    categories = {
        "controller": number("monthly_controller_seconds") * number("controller_hourly_usd") / 3600,
        "inference": messages
        * number("inference_seconds_per_completed_message")
        * number("worker_hourly_usd")
        / 3600,
        "startup_teardown": messages
        * number("startup_teardown_seconds_per_completed_message")
        * number("worker_hourly_usd")
        / 3600,
        "retries": messages
        * number("retry_seconds_per_completed_message")
        * number("worker_hourly_usd")
        / 3600,
        "database": number("monthly_database_usd"),
        "storage": number("monthly_storage_usd"),
        "transfer": number("monthly_transfer_usd"),
    }
    total = sum(categories.values())
    budget = number("monthly_budget_usd")
    if budget <= 0:
        raise ValueError("Monthly budget must be positive")
    return {
        "categories_usd": categories,
        "monthly_total_usd": total,
        "cost_per_completed_message_usd": total / messages if messages else None,
        "budget_usd": budget,
        "budget_alert": "limit"
        if total >= budget
        else "warning"
        if total >= budget * 0.8
        else None,
        "measurement_basis": data.get("measurement_basis", "unspecified"),
        "production_budget_approved": data.get("owner_budget_approved") is True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("measurements", type=Path)
    args = parser.parse_args()
    print(json.dumps(forecast(json.loads(args.measurements.read_text())), indent=2))
