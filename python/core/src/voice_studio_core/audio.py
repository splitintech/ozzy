"""Bounded FFmpeg decoding/encoding; no shell, PyTorch or NumPy dependency."""

import array
import json
import math
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from .contracts import DomainError
from .storage import sha256

PREPARATION_VERSION = "1"
PROCESSING_VERSION = "1"


def executable(name):
    directory = Path(os.environ.get("VOICE_FFMPEG_DIR", str(Path(sys.executable).parent / "bin")))
    bundled = directory / (name + (".exe" if os.name == "nt" else ""))
    found = str(bundled) if bundled.exists() else shutil.which(name)
    if not found:
        raise DomainError("audio_tool_missing", f"{name} is not installed", 503)
    return found


def run(args, timeout=90):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DomainError(
            "decode_timeout", "Audio processing exceeded its time limit", 422
        ) from exc
    if result.returncode:
        raise DomainError("invalid_audio", "The audio could not be decoded or processed", 422)
    return result


def probe(path):
    result = run(
        [
            executable("ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout=15,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(audio) != 1 or any(s.get("codec_type") not in {"audio", "video"} for s in streams):
        raise DomainError("invalid_audio", "Import a recording with one audio track", 422)
    stream = audio[0]
    duration = float(payload["format"].get("duration", stream.get("duration", 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise DomainError("invalid_audio", "The recording has no usable duration", 422)
    return {
        "duration": duration,
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "codec": stream["codec_name"],
        "format": payload["format"]["format_name"],
    }


def samples(path):
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getnchannels() != 1:
            raise DomainError("invalid_audio", "Expected mono PCM16 audio", 422)
        data = array.array("h", source.readframes(source.getnframes()))
        if sys.byteorder != "little":
            data.byteswap()
        return data, source.getframerate()


def prepare_reference(source, destination, start, end, max_seconds=600):
    info = probe(source)
    if info["format"] not in {"mp3", "wav"}:
        raise DomainError("unsupported_audio", "Use an MP3 or WAV recording", 415)
    if info["duration"] > max_seconds:
        raise DomainError(
            "reference_too_long", f"Reference files may be at most {max_seconds}s", 422
        )
    if not all(math.isfinite(x) for x in [start, end]):
        raise DomainError("invalid_excerpt", "Choose a finite excerpt", 422)
    if start < 0 or end > info["duration"] + 0.05 or end - start <= 5 or end - start > 30:
        raise DomainError("invalid_excerpt", "Select more than 5 and at most 30 seconds", 422)
    run(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            str(start),
            "-t",
            str(end - start),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )
    pcm, rate = samples(destination)
    if len(pcm) <= 5 * rate:
        raise DomainError(
            "reference_too_short", "The decoded reference must exceed five seconds", 422
        )
    rms = math.sqrt(sum(float(x) ** 2 for x in pcm) / len(pcm)) / 32768
    if rms < 0.001:
        raise DomainError("silent_reference", "Choose an excerpt with audible speech", 422)
    clipped = sum(abs(x) >= 32760 for x in pcm) / len(pcm)
    silence = sum(abs(x) < 100 for x in pcm) / len(pcm)
    return {
        **info,
        "excerpt_start": start,
        "excerpt_end": end,
        "reference_duration": len(pcm) / rate,
        "clipped_fraction": clipped,
        "silence_fraction": silence,
        "rms_dbfs": 20 * math.log10(rms),
        "warnings": (
            (["The excerpt contains clipping. Consider another take."] if clipped > 0.001 else [])
            + (["The excerpt contains substantial silence."] if silence > 0.5 else [])
        ),
        "preparation_version": PREPARATION_VERSION,
        "original_sha256": sha256(source),
    }


def verify_decodable(path):
    return run(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ]
    )


def loudness(path):
    analysis = run([
        executable("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path),
        "-af", "loudnorm=I=-19:TP=-1:LRA=11:print_format=json", "-f", "null", "-",
    ])
    text = analysis.stderr.decode(errors="replace")
    result = json.loads(text[text.rfind("{"):text.rfind("}") + 1])
    if not all(math.isfinite(float(result[k])) for k in ["input_i", "input_tp", "input_lra"]):
        raise DomainError("silent_output", "The generated take is silent or too short to measure", 422)
    return result


def finish_audio(master, directory):
    directory = Path(directory)
    info = probe(master)
    if info["duration"] > 120.5:
        raise DomainError(
            "message_too_long", "This take exceeds two minutes. Shorten the script.", 422
        )
    if info["duration"] < 0.2:
        raise DomainError("empty_output", "The model returned no usable speech", 422)
    rate = info["sample_rate"]
    measurement = loudness(master)
    normalizer = (
        "loudnorm=I=-19:TP=-1:LRA=11:"
        f"measured_I={measurement['input_i']}:measured_TP={measurement['input_tp']}:"
        f"measured_LRA={measurement['input_lra']}:measured_thresh={measurement['input_thresh']}:"
        f"offset={measurement['target_offset']}:linear=true"
    )
    wav = directory / "speech.wav"
    run(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(master),
            "-af",
            f"afade=t=in:d=0.005,afade=t=out:st={max(0, info['duration'] - 0.005)}:d=0.005,{normalizer}",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-c:a",
            "pcm_s16le",
            str(wav),
        ]
    )
    outputs = [
        ("wav", wav, "audio/wav"),
        ("mp3", directory / "speech.mp3", "audio/mpeg"),
        ("telephone", directory / "telephone.wav", "audio/wav"),
        ("master", Path(master), "audio/wav"),
    ]
    run(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(wav),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(directory / "speech.mp3"),
        ]
    )
    run(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(wav),
            "-ac",
            "1",
            "-ar",
            "8000",
            "-c:a",
            "pcm_s16le",
            str(directory / "telephone.wav"),
        ]
    )
    result = []
    for kind, path, mime in outputs:
        measurement = loudness(path)
        # Lossy encoding and sample-rate conversion can introduce true-peak overshoot.
        # Correct from the finished lossless source, then decode and remeasure.
        if kind != "master" and float(measurement["input_tp"]) > -0.98:
            gain = -1.2 - float(measurement["input_tp"])
            corrected = directory / ("corrected" + path.suffix)
            encoding = (["-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k"] if kind == "mp3"
                        else ["-ar", "8000" if kind == "telephone" else str(rate), "-c:a", "pcm_s16le"])
            run([executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(path),
                 "-af", f"volume={gain}dB", "-ac", "1", *encoding, str(corrected)])
            os.replace(corrected, path)
            measurement = loudness(path)
            if float(measurement["input_tp"]) > -0.95:
                raise DomainError("export_peak", "The export exceeded the true-peak ceiling", 500)
        verify_decodable(path)
        output_info = probe(path)
        if abs(output_info["duration"] - info["duration"]) > 0.2:
            raise DomainError("export_incomplete", "Export duration does not match the take", 500)
        result.append({"kind": kind, "path": str(path), "mime_type": mime, **output_info,
                       "integrated_lufs": float(measurement["input_i"]),
                       "true_peak_dbtp": float(measurement["input_tp"])})
    return result
