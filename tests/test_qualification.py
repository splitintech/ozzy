import importlib.util
from pathlib import Path

import pytest


def module(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / "scripts" / f"{name}.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_qualification_requires_complete_human_and_hosted_evidence():
    check = module("check_qualification")
    assert check.evaluate([])
    rows = [
        {
            "script_id": str(i),
            "seed": seed,
            "naturalness": 4,
            "similarity": 4,
            "critical_correct": True,
            "complete": True,
            "owner_approved": True,
            "unwanted_repetition": False,
            "output_seconds": 40,
        }
        for i in range(20)
        for seed in [1, 2, 3]
    ]
    assert check.evaluate(rows)  # Even perfect review scores do not prove hosted performance.
    hosted = {
        "ready_to_artifacts_seconds": [29] * 60,
        "recovery_passed": True,
        "start_generate_download_stop_passed": True,
        "cost_accounting_complete": True,
        "owner_budget_approved": True,
    }
    assert check.evaluate(rows, hosted) == []
    rows[0]["critical_correct"] = False
    assert check.evaluate(rows, hosted)


def test_cost_forecast_counts_all_cost_categories_and_rejects_missing_costs():
    cost = module("cost_forecast")
    data = {
        "monthly_completed_messages": 10,
        "monthly_controller_seconds": 3600,
        "controller_hourly_usd": 1,
        "inference_seconds_per_completed_message": 360,
        "worker_hourly_usd": 2,
        "startup_teardown_seconds_per_completed_message": 180,
        "retry_seconds_per_completed_message": 180,
        "monthly_database_usd": 3,
        "monthly_storage_usd": 4,
        "monthly_transfer_usd": 5,
        "monthly_budget_usd": 20,
    }
    result = cost.forecast(data)
    assert result["monthly_total_usd"] == 17
    assert result["budget_alert"] == "warning"
    assert result["production_budget_approved"] is False
    del data["monthly_transfer_usd"]
    with pytest.raises(ValueError):
        cost.forecast(data)
