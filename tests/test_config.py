import pytest
from pydantic import ValidationError
from voice_studio_core.config import Settings


@pytest.mark.parametrize("values", [
    {"monthly_budget_usd": float("nan")}, {"monthly_budget_usd": float("inf")},
    {"job_reservation_usd": -1}, {"job_reservation_usd": 0},
    {"worker_hourly_usd": -1}, {"controller_hourly_usd": float("nan")},
    {"retention_days": 31}, {"retention_days": 0}, {"lease_seconds": 0},
    {"job_timeout_seconds": -1}, {"max_queue": 0}, {"port": 65536},
])
def test_invalid_lifecycle_and_budget_configuration_fails_before_start(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
