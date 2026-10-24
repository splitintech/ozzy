"""Frozen CLI entrypoint, including the isolated inference child process."""

import multiprocessing
import os
import sys
from pathlib import Path


def main():
    multiprocessing.freeze_support()
    if getattr(sys, "frozen", False):
        internal = Path(sys._MEIPASS)
        os.environ.setdefault("VOICE_FFMPEG_DIR", str(internal / "ffmpeg"))
        os.environ.setdefault("VOICE_STATIC_DIR", str(internal / "studio"))
    if sys.argv[1:2] == ["--runtime-self-test"]:
        import json
        import subprocess

        import torch
        import torchaudio
        from chatterbox.tts import ChatterboxTTS
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        from voice_studio_engine.adapter import ChatterboxEngine

        assert all(
            callable(value) for value in (ChatterboxTTS, ChatterboxTurboTTS, ChatterboxEngine)
        )
        assert torchaudio.__version__
        suffix = ".exe" if os.name == "nt" else ""
        for binary in ("ffmpeg", "ffprobe"):
            subprocess.run(
                [str(Path(os.environ["VOICE_FFMPEG_DIR"]) / (binary + suffix)), "-version"],
                check=True,
                capture_output=True,
            )
        print(
            json.dumps(
                {
                    "packaged_imports": True,
                    "ffmpeg": True,
                    "torch": torch.__version__,
                    "model_inference_tested": False,
                }
            )
        )
        return
    if sys.argv[1:2] == ["--engine-runner"]:
        from voice_studio_engine.runner import main as runner

        runner()
    else:
        from voice_studio_cli.main import main as cli

        cli()


if __name__ == "__main__":
    main()
