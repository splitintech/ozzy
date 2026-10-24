"""Private JSON-lines protocol. Never receives URLs or executes user-supplied code."""

import contextlib
import json
import os
import sys

from voice_studio_core.contracts import DomainError


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    protocol = sys.stdout

    def emit(message):
        protocol.write(json.dumps(message) + "\n")
        protocol.flush()

    with contextlib.redirect_stdout(sys.stderr):
        from .adapter import ChatterboxEngine

        engine = ChatterboxEngine()
    for line in sys.stdin:
        try:
            payload = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                metadata = engine.generate(payload, lambda state: emit({"state": state}))
            emit({"event": "completed", "metadata": metadata})
        except DomainError as exc:
            emit({"event": "failed", "code": exc.code, "message": exc.message})
        except ImportError:
            emit(
                {
                    "event": "failed",
                    "code": "engine_not_installed",
                    "message": "Install the inference runtime before generating speech.",
                }
            )
        except Exception:
            # Do not leak script, paths, storage credentials or upstream exceptions.
            emit(
                {
                    "event": "failed",
                    "code": "inference_failed",
                    "message": "The model could not complete this take. Try a shorter script or CPU.",
                }
            )


if __name__ == "__main__":
    main()
