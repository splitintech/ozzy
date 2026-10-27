"""Fixed local owner-key deadlines, including restart and launcher bypass cases."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from voice_studio_core.config import Settings
from voice_studio_core.database import Credential
from voice_studio_service import auth
from voice_studio_service.api import create_app


@pytest.fixture
def clock(monkeypatch):
    value = [1_800_000_000.0, 100.0]
    monkeypatch.setattr(
        auth, "time", SimpleNamespace(time=lambda: value[0], monotonic=lambda: value[1])
    )
    return value


def browser(config, repo, objects):
    return TestClient(
        create_app(config, repo, objects),
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 1234),
    )


def headers(token="test-local-token"):
    return {"Authorization": f"Bearer {token}", "Origin": "http://127.0.0.1:8765"}


def test_exact_deadline_rejects_reads_mutations_and_launcher(config, repo, objects, clock):
    with browser(config, repo, objects) as client:
        deadline = client.get("/v1/session").json()["local_access_expires_at"]
        assert deadline == clock[0] + 8 * 3600
        assert "test-local-token" not in client.get("/v1/session").text
        clock[0] = deadline - 0.01
        assert client.get("/v1/voices", headers=headers()).status_code == 200
        assert client.get("/launcher/deployments", headers=headers()).status_code == 200
        clock[0] = deadline
        for method, path, body in [
            ("GET", "/v1/voices", None),
            ("GET", "/v1/artifacts/missing", None),
            ("POST", "/v1/keys", {"name": "expired"}),
            ("DELETE", "/v1/voices/missing", None),
            ("GET", "/launcher/deployments", None),
            ("GET", "/launcher/operations/missing", None),
            ("POST", "/launcher/operations", {"action": "start"}),
        ]:
            response = client.request(method, path, json=body, headers=headers())
            assert response.status_code == 401, response.text
            assert response.json()["code"] == "local_access_expired"
        assert client.get("/health").status_code == 200
        with repo.tx() as session:
            credential = session.scalar(select(Credential))
            assert credential.digest == auth.digest("test-local-token")
            assert credential.expires_at == deadline
            assert credential.kind == "local_access"


def test_restart_never_renews_same_key_or_extends_configured_lifetime(config, repo, objects, clock):
    with browser(config, repo, objects) as client:
        deadline = client.get("/v1/session").json()["local_access_expires_at"]
    clock[0] += 3600
    config.local_token_ttl_seconds = 86400
    with browser(config, repo, objects) as client:
        assert client.get("/v1/session").json()["local_access_expires_at"] == deadline
        assert client.get("/v1/voices", headers=headers()).status_code == 200
    clock[0] = deadline
    with browser(config, repo, objects) as client:
        assert client.get("/v1/session").json()["local_access_expires_at"] == deadline
        assert client.get("/v1/voices", headers=headers()).status_code == 401


def test_rotation_retires_previous_key_without_api_fallback(config, repo, objects, clock):
    with browser(config, repo, objects) as client:
        integration = client.post(
            "/v1/keys", json={"name": "Integration"}, headers=headers()
        ).json()["key"]
    config.local_token = SecretStr("fresh-test-key")
    with browser(config, repo, objects) as client:
        assert client.get("/v1/voices", headers=headers("fresh-test-key")).status_code == 200
        assert client.get("/v1/voices", headers=headers()).status_code == 401
        # Old local records cannot masquerade as API credentials, even if unrevoked.
        with repo.tx(lock=True) as session:
            session.scalar(
                select(Credential).where(
                    Credential.kind == "local_access",
                    Credential.digest == auth.digest("test-local-token"),
                )
            ).revoked = False
        assert client.get("/v1/voices", headers=headers()).status_code == 401
        assert client.get("/v1/voices", headers=headers(integration)).status_code == 200
        assert client.get("/launcher/deployments", headers=headers(integration)).status_code == 401
    # Starting another launch retires the old key again; it cannot later be resurrected.
    with browser(config, repo, objects):
        pass
    config.local_token = SecretStr("test-local-token")
    with browser(config, repo, objects) as client:
        assert client.get("/v1/voices", headers=headers()).status_code == 401


def test_wall_clock_rollback_does_not_extend_running_key(config, repo, objects, clock):
    config.local_token_ttl_seconds = 60
    with browser(config, repo, objects) as client:
        clock[0] -= 3600
        clock[1] += 60
        assert client.get("/v1/voices", headers=headers()).status_code == 401


def test_shorter_lifetime_takes_effect_without_changing_first_issue_time(
    config, repo, objects, clock
):
    issued_at = clock[0]
    with browser(config, repo, objects):
        pass
    clock[0] += 100
    config.local_token_ttl_seconds = 60
    with browser(config, repo, objects) as client:
        assert client.get("/v1/session").json()["local_access_expires_at"] == issued_at + 60
        assert client.get("/v1/voices", headers=headers()).status_code == 401


@pytest.mark.parametrize("ttl", [0, -1, 59, 86401, float("inf")])
def test_no_unlimited_or_excessive_lifetime(ttl):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, local_token_ttl_seconds=ttl)
