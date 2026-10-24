"""Disposable loopback service for browser tests. It does not perform voice synthesis.

Only this script injects the synthetic fixture engine into the real API/worker.
It is never part of the application runtime or release packaging.
"""

import array
import math
import queue
import shutil
import tempfile
import wave
from pathlib import Path

import uvicorn
from voice_studio_core.config import Settings
from voice_studio_service.api import create_app


class BrowserFixtureEngine:
    def __init__(self, recording):
        self.recording = recording
        self.events = queue.Queue()
        self.process = None

    def send(self, payload):
        shutil.copyfile(self.recording, Path(payload["output"]) / "master.wav")
        self.events.put({"state": "generating"})
        self.events.put(
            {
                "event": "completed",
                "metadata": {
                    "duration": 8,
                    "timings": {"generation_seconds": 0.01},
                    "test_fixture": True,
                },
            }
        )

    def stop(self):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix="voice-studio-browser-test-") as temporary:
        root = Path(temporary)
        recording = root / "synthetic-test-tone.wav"
        samples = array.array(
            "h",
            (
                int(
                    6500
                    * math.sin(2 * math.pi * 180 * index / 24000)
                    * (0.6 + 0.4 * math.sin(index / 1700) ** 2)
                )
                for index in range(8 * 24000)
            ),
        )
        with wave.open(str(recording), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(samples.tobytes())
        settings = Settings(
            _env_file=None,
            data_dir=root / "data",
            model_dir=root / "models",
            local_token="test-local-token",
            port=18765,
            worker_enabled=True,
            static_dir=Path(__file__).resolve().parents[1] / "apps/studio/dist",
        )
        app = create_app(settings, engine=BrowserFixtureEngine(recording))
        uvicorn.run(app, host="127.0.0.1", port=18765, log_level="warning")


if __name__ == "__main__":
    main()
