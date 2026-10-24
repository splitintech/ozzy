"""Measure Perth response before/after export; this is not a universal detection claim."""

import argparse
import json
import os
import socket
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")

    def offline(*_, **__):
        raise RuntimeError("Network access is disabled for this regression")

    socket.socket.connect = offline
    socket.create_connection = offline
    import numpy as np
    import perth
    import soundfile as sf
    import torch
    from voice_studio_core.audio import executable, run

    torch.set_num_threads(4)
    detector = perth.PerthImplicitWatermarker(device="cpu")
    rows = []
    for name in ["master.wav", "speech.wav", "speech.mp3", "telephone.wav"]:
        path = args.directory / name
        # Decode MP3 using the same bundled FFmpeg required by the application.
        if path.suffix == ".mp3":
            decoded = args.directory / "watermark-decoded.wav"
            run(
                [
                    executable("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-c:a",
                    "pcm_f32le",
                    str(decoded),
                ]
            )
            try:
                waveform, rate = sf.read(decoded, dtype="float32")
            finally:
                decoded.unlink(missing_ok=True)
        else:
            waveform, rate = sf.read(path, dtype="float32")
        with torch.inference_mode():
            response = np.asarray(detector.get_watermark(waveform, rate, round=False))
        if not np.isfinite(response).all():
            raise RuntimeError(f"Non-finite detector response for {name}")
        rows.append(
            {
                "file": name,
                "sample_rate": rate,
                "response_mean": float(response.mean()),
                "response_min": float(response.min()),
                "response_max": float(response.max()),
                "network_blocked": True,
            }
        )
    result = {
        "method": "Pinned Perth implicit decoder, unrounded response",
        "interpretation": "Observed response only; no calibrated detection guarantee",
        "exports": rows,
    }
    (args.directory / "watermark-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
