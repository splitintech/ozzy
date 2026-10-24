"""Offline inference wiring smoke with synthetic reference audio, never voice qualification."""

import argparse
import hashlib
import json
import os
import socket
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/engine-conditioning"))
    parser.add_argument(
        "--synthetic-source",
        type=Path,
        required=True,
        help="Previously generated synthetic WAV; never supply someone else's recording",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")

    def offline(*_, **__):
        raise RuntimeError("Network access disabled during the inference smoke")

    socket.socket.connect = offline
    socket.create_connection = offline
    import numpy as np
    import soundfile as sf
    import torch
    from voice_studio_core.audio import finish_audio, prepare_reference
    from voice_studio_engine.adapter import ChatterboxEngine

    torch.set_num_threads(4)
    source, rate = sf.read(args.synthetic_source, dtype="float32", always_2d=True)
    source = source.mean(axis=1)
    # Repetition deliberately creates a plumbing fixture, not an audition-quality reference.
    synthetic = np.tile(source, max(1, int(np.ceil(rate * 12 / len(source)))))[: rate * 12]
    raw = args.output / "synthetic-source.wav"
    reference = args.output / "reference.wav"
    sf.write(raw, synthetic, rate)
    prepare_reference(raw, reference, 0, min(12, len(synthetic) / rate))
    began = time.monotonic()
    metadata = ChatterboxEngine().generate(
        {
            "reference": str(reference),
            "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "model_dir": str(args.model_dir),
            "device": "cpu",
            "cpu_threads": 4,
            "voice_id": "synthetic-plumbing-fixture",
            "text": "Hello. Please leave a message after the tone.",
            "options": {"model": "nano", "seed": 42},
            "output": str(args.output),
        },
        lambda phase: print(json.dumps({"phase": phase}), flush=True),
    )
    exports = finish_audio(args.output / "master.wav", args.output)
    report = {
        "network_blocked": True,
        "reference_kind": "repeated application-generated synthetic audio",
        "owner_voice_tested": False,
        "quality_qualified": False,
        "hosted_qualified": False,
        "total_seconds": time.monotonic() - began,
        "metadata": metadata,
        "exports": [{key: value for key, value in row.items() if key != "path"} for row in exports],
    }
    (args.output / "smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
