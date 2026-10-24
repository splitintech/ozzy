import sys
from types import SimpleNamespace

import pytest
from voice_studio_engine.adapter import ChatterboxEngine


def test_failed_model_switch_does_not_reuse_an_unloaded_cached_model(monkeypatch, tmp_path):
    from voice_studio_engine import adapter

    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    )
    original_instances = []

    def original_load(*_, **__):
        instance = object()
        original_instances.append(instance)
        return instance

    def broken_load(*_, **__):
        raise RuntimeError("Simulated model initialization failure")

    monkeypatch.setitem(
        sys.modules,
        "chatterbox.tts",
        SimpleNamespace(ChatterboxTTS=SimpleNamespace(from_local=original_load)),
    )
    monkeypatch.setitem(
        sys.modules,
        "chatterbox.tts_turbo",
        SimpleNamespace(ChatterboxTurboTTS=SimpleNamespace(from_local=broken_load)),
    )
    monkeypatch.setattr(adapter, "verify_model", lambda *_: True)
    monkeypatch.setattr(adapter, "model_path", lambda root, name: root / name)
    engine = ChatterboxEngine()
    first = engine.load(tmp_path, "original", "cpu")
    assert engine.load(tmp_path, "original", "cpu") is first
    with pytest.raises(RuntimeError, match="initialization failure"):
        engine.load(tmp_path, "nano", "cpu")
    recovered = engine.load(tmp_path, "original", "cpu")
    assert recovered is not None and recovered is not first
    assert len(original_instances) == 2
