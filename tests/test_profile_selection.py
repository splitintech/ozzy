import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from select_profile import select_profile  # noqa: E402
from voice_studio_engine.models import model_lock  # noqa: E402


def candidate(name, cost, device="cpu", warm=20):
    return {"id": name, "benchmark": {"model": "nano", "device": device,
            "cpu_threads": 4, "pins": model_lock()},
            "scores": [{"script_id": str(i), "seed": seed, "output_seconds": 40,
                        "naturalness": 4, "similarity": 4, "critical_correct": True,
                        "complete": True, "owner_approved": True, "unwanted_repetition": False}
                       for i in range(20) for seed in range(3)],
            "hosted": {"ready_to_artifacts_seconds": [warm] * 60, "recovery_passed": True,
                       "start_generate_download_stop_passed": True,
                       "cost_accounting_complete": True, "owner_budget_approved": True},
            "cost": {"cost_per_completed_message_usd": cost}}


def test_cheapest_qualified_cpu_wins_over_faster_or_cheaper_gpu():
    report = select_profile([candidate("cpu4", 0.2), candidate("cpu2", 0.1),
                             candidate("gpu", 0.01, "cuda")])
    assert report["selected"] == "cpu2"
    assert report["stable_release_approved"] is False


def test_gpu_requires_measured_cpu_failure_not_missing_owner_review():
    cpu = candidate("cpu", 0.1, warm=31)
    gpu = candidate("gpu", 0.2, "cuda")
    assert select_profile([cpu, gpu])["selected"] == "gpu"
    cpu["scores"][0]["owner_approved"] = None
    assert select_profile([cpu, gpu])["selected"] is None


def test_missing_evidence_nonfinite_cost_and_unverified_quota_do_not_qualify():
    original = candidate("fixture", 0.2)
    for changes in [{"cost": {"cost_per_completed_message_usd": float("nan")}},
                    {"hosted": None}, {"benchmark": {"model": "nano", "device": "cpu"}},
                    {"benchmark": {**original["benchmark"], "cpu_threads": 8}}]:
        value = copy.deepcopy(original)
        value.update(changes)
        assert select_profile([value])["selected"] is None
