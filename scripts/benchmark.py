"""Owner-authorized qualification. Generated audio and reference paths never enter Git."""

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path

from voice_studio_core.audio import finish_audio, prepare_reference, probe
from voice_studio_engine.adapter import ChatterboxEngine
from voice_studio_engine.models import model_lock


def peak_memory_bytes():
    if platform.system() != "Windows":
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss if platform.system() == "Darwin" else rss * 1024
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in [
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            ]
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError("Cannot read process peak memory")
    return counters.PeakWorkingSetSize


def percentile95(values):
    return sorted(values)[max(0, __import__("math").ceil(len(values) * 0.95) - 1)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--authorized", action="store_true", required=True)
    parser.add_argument("--model", choices=["original", "turbo", "nano"], default="nano")
    parser.add_argument("--device", choices=["cpu", "mps", "cuda"], default="cpu")
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmark"))
    parser.add_argument("--seeds", default="42,123,2026")
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--hourly-usd", type=float, default=0)
    parser.add_argument("--scripts", type=Path, default=Path("benchmarks/scripts.json"))
    args = parser.parse_args()
    import torch

    torch.set_num_threads(args.cpu_threads)
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    reference = args.output / "reference.wav"
    info = probe(args.reference)
    prepare_reference(args.reference, reference, 0, min(15, info["duration"]))
    scripts = json.loads(args.scripts.read_text())
    engine = ChatterboxEngine()
    payload = {
        "reference": str(reference),
        "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "model_dir": str(args.model_dir),
        "device": args.device,
        "cpu_threads": args.cpu_threads,
        "voice_id": "qualification-reference",
    }
    cold = engine.generate(
        {
            **payload,
            "text": "Hello. This is a readiness check.",
            "options": {"model": args.model},
            "output": str(args.output / "warmup"),
        },
        lambda _: None,
    )
    rows = []
    for script in scripts:
        for seed in [int(x) for x in args.seeds.split(",")]:
            directory = args.output / f"{script['id']}-{seed}"
            begin = time.monotonic()
            result = engine.generate(
                {
                    **payload,
                    "text": script["text"],
                    "options": {"model": args.model, "seed": seed},
                    "output": str(directory),
                },
                lambda _: None,
            )
            finish_audio(directory / "master.wav", directory)
            duration = time.monotonic() - begin
            rows.append(
                {
                    "script_id": script["id"],
                    "seed": seed,
                    "warm_seconds": duration,
                    "output_seconds": result["duration"],
                    "timings": result["timings"],
                    "naturalness": None,
                    "similarity": None,
                    "critical_correct": None,
                    "complete": None,
                    "unwanted_repetition": None,
                    "owner_approved": None,
                }
            )
            (args.output / "scores.json").write_text(json.dumps(rows, indent=2) + "\n")
            print(
                json.dumps(
                    {"script_id": script["id"], "seed": seed, "seconds": round(duration, 3)}
                ),
                flush=True,
            )
    elapsed = time.monotonic() - started
    rss = peak_memory_bytes()
    report = {
        "schema": 1,
        "hardware": platform.platform(),
        "machine": platform.machine(),
        "cpu_threads": args.cpu_threads,
        "device": args.device,
        "torch": torch.__version__,
        "model": args.model,
        "pins": model_lock(),
        "cold_engine": cold["timings"],
        "warm_p95_seconds": percentile95([r["warm_seconds"] for r in rows]),
        "peak_rss_bytes": rss,
        "total_runtime_seconds": elapsed,
        "estimated_compute_usd": elapsed * args.hourly_usd / 3600,
        "includes": ["reference preparation", "resident model generation", "audio finishing"],
        "excludes": [
            "sandbox startup",
            "S3 upload",
            "controller",
            "database",
            "storage",
            "transfer",
            "teardown",
        ],
        "hosted_qualified": False,
        "quality_review_pending": True,
        "records": len(rows),
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
