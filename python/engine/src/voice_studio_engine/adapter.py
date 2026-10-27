import json
import random
import threading
import time
from pathlib import Path

from voice_studio_core.contracts import DomainError, SynthesisOptions
from voice_studio_core.storage import sha256

from .models import model_lock, model_path, verify_model
from .text import segments


class ChatterboxEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.loaded = None
        self.identity = None
        self.conditioning = None
        self.verified = set()

    def load(self, model_dir, name, device):
        with self.lock:
            return self._load(model_dir, name, device)

    def _load(self, model_dir, name, device):
        import torch

        if device == "mps" and not torch.backends.mps.is_available():
            raise DomainError("device_unavailable", "MPS is unavailable; choose local CPU", 422)
        if device == "cuda" and not torch.cuda.is_available():
            raise DomainError("device_unavailable", "CUDA is unavailable; choose CPU", 422)
        path = model_path(model_dir, name)
        identity = (str(path), name, device)
        if identity not in self.verified:
            if not verify_model(model_dir, name):
                raise DomainError("model_missing", "Install and verify this model in Settings", 409)
            self.verified.add(identity)
        if self.identity == identity:
            return self.loaded
        self.loaded = None
        self.identity = None
        self.conditioning = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if name == "original":
            from chatterbox.tts import ChatterboxTTS

            model = ChatterboxTTS.from_local(path, device=device)
        else:
            from chatterbox.tts_turbo import ChatterboxTurboTTS

            model = ChatterboxTurboTTS.from_local(path, device=device, nano=name == "nano")
        self.loaded, self.identity = model, identity
        return model

    def generate(self, payload, progress):
        with self.lock:
            return self._generate(payload, progress)

    def _generate(self, payload, progress):
        import numpy as np
        import torch

        options = SynthesisOptions.model_validate(payload["options"])
        torch.set_num_threads(int(payload.get("cpu_threads", 4)))
        started = time.monotonic()
        progress("loading")
        model = self.load(payload["model_dir"], options.model, payload["device"])
        loaded_at = time.monotonic()
        reference = Path(payload["reference"])
        if sha256(reference) != payload["reference_sha256"]:
            raise DomainError("reference_integrity", "Reference checksum mismatch", 422)
        conditioning = (
            str(self.identity),
            payload["reference_sha256"],
            options.exaggeration,
        )
        if self.conditioning != conditioning:
            self.conditioning = None
            kwargs = {
                "exaggeration": options.exaggeration if options.exaggeration is not None else 0.5
            }
            model.prepare_conditionals(str(reference), **kwargs)
            self.conditioning = conditioning
        ready = time.monotonic()
        progress("generating")
        chunks = segments(payload["text"])
        generated = []
        durations = []
        for index, text in enumerate(chunks):
            seed = (options.seed + index) % (2**32)
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            kwargs = {"temperature": options.temperature}
            if options.model == "original":
                kwargs.update(
                    {
                        "exaggeration": options.exaggeration
                        if options.exaggeration is not None
                        else 0.5,
                        "cfg_weight": options.cfg_weight if options.cfg_weight is not None else 0.5,
                    }
                )
            with torch.inference_mode():
                audio = model.generate(text, **kwargs).detach().cpu().numpy().reshape(-1)
            if not np.isfinite(audio).all() or len(audio) < model.sr * 0.1:
                raise DomainError("invalid_model_output", "The model returned an invalid take", 422)
            durations.append(len(audio) / model.sr)
            # Join only at sentence/word boundaries; never crop generated content.
            generated.append(audio.astype(np.float32))
            if index < len(chunks) - 1:
                generated.append(np.zeros(int(model.sr * 0.12), dtype=np.float32))
            if sum(len(a) for a in generated) / model.sr > 120.5:
                raise DomainError(
                    "message_too_long", "This take exceeds two minutes. Shorten the script.", 422
                )
        pcm = np.concatenate(generated)
        output = Path(payload["output"])
        output.mkdir(parents=True, exist_ok=True)
        master = output / "master.wav"
        import soundfile as sf
        # Preserve the unprocessed model waveform. Export finishing is separate.
        sf.write(master, pcm, model.sr, subtype="FLOAT")
        ended = time.monotonic()
        spec = model_lock()
        metadata = {
            "model": options.model,
            "model_revision": spec["models"][options.model]["revision"],
            "chatterbox_commit": spec["chatterbox_commit"],
            "voice_id": payload["voice_id"],
            "reference_sha256": payload["reference_sha256"],
            "options": options.model_dump(),
            "device": payload["device"],
            "sample_rate": model.sr,
            "duration": len(pcm) / model.sr,
            "segment_count": len(chunks),
            "segment_durations": durations,
            "watermark": "upstream-perth-enabled",
            "timings": {
                "model_load_seconds": loaded_at - started,
                "reference_preparation_seconds": ready - loaded_at,
                "load_and_reference_seconds": ready - started,
                "generation_seconds": ended - ready,
                "engine_seconds": ended - started,
            },
        }
        (output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
        return metadata
