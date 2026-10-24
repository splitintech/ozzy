import subprocess
import wave

import pytest
from voice_studio_core.audio import executable, finish_audio, prepare_reference, probe
from voice_studio_core.contracts import DomainError


def test_real_mp3_decode_and_exports(recording, tmp_path):
    mp3 = tmp_path / "input.mp3"
    subprocess.run(
        [executable("ffmpeg"), "-v", "error", "-y", "-i", str(recording), str(mp3)], check=True
    )
    prepared = tmp_path / "prepared.wav"
    details = prepare_reference(mp3, prepared, 0, 7)
    assert details["reference_duration"] == pytest.approx(7, abs=0.05)
    exports = {item["kind"]: item for item in finish_audio(prepared, tmp_path)}
    assert exports["mp3"]["sample_rate"] == 44100
    for kind in ["wav", "mp3", "telephone"]:
        assert exports[kind]["true_peak_dbtp"] <= -0.95
        assert exports[kind]["integrated_lufs"] == pytest.approx(-19, abs=1.0)
    assert exports["wav"]["sample_rate"] == 24000
    assert exports["telephone"]["sample_rate"] == 8000
    assert all(item["channels"] == 1 for item in exports.values())
    assert all(item["duration"] == pytest.approx(7, abs=0.2) for item in exports.values())


def test_silent_reference_rejected(tmp_path):
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(b"\x00" * 8 * 24000 * 2)
    with pytest.raises(DomainError, match="audible speech"):
        prepare_reference(path, tmp_path / "prepared.wav", 0, 8)


@pytest.mark.parametrize(
    "start,end", [(0, 5), (-1, 6), (0, 32), (0, float("nan")), (6, 4), (0, 99)]
)
def test_invalid_excerpt(recording, tmp_path, start, end):
    with pytest.raises(DomainError):
        prepare_reference(recording, tmp_path / "prepared.wav", start, end)


def test_malformed_audio_has_useful_error(tmp_path):
    file = tmp_path / "not-a-recording.mp3"
    file.write_text("not audio")
    with pytest.raises(DomainError, match="decoded"):
        probe(file)
