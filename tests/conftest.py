import array
import math
import wave

import pytest
from fastapi.testclient import TestClient
from voice_studio_core.config import Settings
from voice_studio_core.database import Repository
from voice_studio_core.storage import LocalStorage
from voice_studio_service.api import create_app


@pytest.fixture
def config(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
        local_token="test-local-token",
        worker_enabled=False,
    ).prepare()


@pytest.fixture
def repo(config):
    repository = Repository(config)
    repository.initialize()
    yield repository
    repository.dispose()


@pytest.fixture
def objects(config):
    return LocalStorage(config.data_dir / "objects")


@pytest.fixture
def client(config, repo, objects):
    with TestClient(create_app(config, repo, objects), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer test-local-token"
        yield client


@pytest.fixture
def recording(tmp_path):
    """Synthetic tone for decoding/queue tests only. Never used to qualify a voice."""
    path = tmp_path / "reference.wav"
    samples = array.array(
        "h",
        (
            int(
                6500
                * math.sin(2 * math.pi * 180 * i / 24000)
                * (0.6 + 0.4 * math.sin(i / 1700) ** 2)
            )
            for i in range(8 * 24000)
        ),
    )
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(samples.tobytes())
    return path


@pytest.fixture
def voice(client, recording):
    response = client.post(
        "/v1/voices",
        files={"file": ("sample.wav", recording.read_bytes(), "audio/wav")},
        data={"name": "Test reference", "authorized": "true", "start": 0, "end": 8},
    )
    assert response.status_code == 201, response.text
    return response.json()
