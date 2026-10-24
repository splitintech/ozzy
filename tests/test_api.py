import sys
import time

from voice_studio_core.database import Artifact, Job
from voice_studio_service.auth import passwords


def audition(client, voice, key="test-submit"):
    return client.post(
        "/v1/generations",
        headers={"Idempotency-Key": key},
        json={
            "voice_id": voice["id"],
            "text": "Audition",
            "purpose": "audition",
            "options": {"model": "nano"},
        },
    )


def test_lightweight_api_and_auth(client):
    assert "torch" not in sys.modules
    assert client.get("/health").status_code == 200
    assert client.get("/v1/capabilities").status_code == 200
    assert client.get("/v1/voices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/voices", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/v1/voices", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_immutable_voice_and_required_audition(client, voice):
    assert not voice["active"]
    response = client.post("/v1/generations", json={"voice_id": voice["id"], "text": "Hello."})
    assert response.status_code == 409
    assert response.json()["code"] == "voice_not_approved"
    response = client.post(
        f"/v1/voices/{voice['id']}/activate", json={"audition_job_id": "missing", "approved": True}
    )
    assert response.status_code == 409


def test_idempotency_and_conflict(client, voice):
    first = audition(client, voice)
    second = audition(client, voice)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["created"] is True and second.json()["created"] is False
    changed = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "test-submit"},
        json={
            "voice_id": voice["id"],
            "text": "Different",
            "purpose": "audition",
            "options": {"seed": 12},
        },
    )
    assert changed.status_code == 409
    assert len(client.get("/v1/generations").json()) == 1


def test_queue_limit_and_cancellation(client, voice, config):
    config.max_queue = 1
    first = audition(client, voice).json()
    assert audition(client, voice, "second").status_code == 429
    assert client.post(f"/v1/generations/{first['id']}/cancel").json()["state"] == "cancelled"
    assert audition(client, voice, "third").status_code == 202


def test_expired_content_is_inaccessible_before_cleanup(client, voice, repo):
    job = audition(client, voice).json()
    with repo.tx(lock=True) as session:
        item = session.get(Job, job["id"])
        item.expires_at = time.time() - 1
        item.state = "completed"
        session.add(
            Artifact(
                id="test-artifact",
                job_id=item.id,
                kind="wav",
                key="expiring/test.wav",
                mime_type="audio/wav",
                checksum="0" * 64,
                size=1,
                duration=1,
            )
        )
    assert client.get(f"/v1/generations/{job['id']}").status_code == 404
    assert client.get("/v1/artifacts/test-artifact").status_code == 404
    assert client.get("/v1/generations").json() == []
    assert audition(client, voice).status_code == 409


def test_voice_delete_removes_references_and_revokes_history(client, voice, objects):
    job = audition(client, voice).json()
    assert client.delete(f"/v1/voices/{voice['id']}").status_code == 200
    assert client.get(f"/v1/generations/{job['id']}").status_code == 404
    assert not objects.path(f"voices/{voice['id']}").exists()
    assert client.get("/v1/voices").json() == []


def test_api_keys_revoke_and_cannot_mint_keys(client):
    created = client.post("/v1/keys", json={"name": "Trusted test client"})
    assert created.status_code in (200, 201)
    key = created.json()
    headers = {"Authorization": f"Bearer {key['key']}"}
    assert client.get("/v1/voices", headers=headers).status_code == 200
    assert client.post("/v1/keys", json={"name": "Forbidden"}, headers=headers).status_code == 403
    assert client.delete(f"/v1/keys/{key['id']}").status_code == 200
    assert client.get("/v1/voices", headers=headers).status_code == 401


def test_hosted_login_throttle_and_csrf(config, repo, objects):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from voice_studio_service.api import create_app

    config.mode = "hosted"
    config.owner_password_hash = SecretStr(passwords.hash("a-strong-test-password"))
    config.session_secret = SecretStr("test-session-secret-at-least-thirty-two-characters")
    with TestClient(create_app(config, repo, objects), base_url="https://studio.test") as browser:
        assert (
            browser.post(
                "/v1/auth/login",
                json={"password": "a-strong-test-password"},
                headers={"Origin": "https://other.test"},
            ).status_code
            == 403
        )
        assert (
            browser.post(
                "/v1/auth/login",
                json={"password": "a-strong-test-password"},
                headers={"Origin": "https://studio.test"},
            ).status_code
            == 200
        )
        assert browser.get("/v1/voices").status_code == 200
        assert browser.post("/v1/activity").status_code == 403
        assert (
            browser.post("/v1/activity", headers={"Origin": "https://studio.test"}).status_code
            == 200
        )
        assert (
            browser.post("/v1/auth/logout", headers={"Origin": "https://studio.test"}).status_code
            == 200
        )
        assert browser.get("/v1/voices").status_code == 401
        for _ in range(5):
            assert browser.post("/v1/auth/login", json={"password": "wrong"}).status_code == 401
        assert browser.post("/v1/auth/login", json={"password": "wrong"}).status_code == 429


def test_owner_activity_is_not_refreshed_by_polling(client, repo):
    from voice_studio_core.database import Runtime

    with repo.tx(lock=True) as s:
        s.get(Runtime, 1).last_activity = 100
    client.get("/v1/capabilities")
    client.get("/v1/generations")
    with repo.tx() as s:
        assert s.get(Runtime, 1).last_activity == 100
    client.post("/v1/activity")
    with repo.tx() as s:
        assert s.get(Runtime, 1).last_activity > 100


def test_error_validation_does_not_return_script(client, voice):
    response = client.post(
        "/v1/generations", json={"voice_id": voice["id"], "text": "<private customer text>"}
    )
    assert response.status_code == 422
    assert "private customer text" not in response.text


def test_request_size_is_bounded_before_multipart_parsing(client, config):
    config.max_upload_bytes = 8
    response = client.post("/v1/voices", content=b"x", headers={"Content-Length": "99999999"})
    assert response.status_code == 413
    response = client.post(
        "/v1/voices",
        content=b"x",
        headers={"Authorization": "Bearer invalid", "Content-Length": "99999999"},
    )
    assert response.status_code == 401
    response = client.post("/v1/auth/login", content=b"x" * 40000)
    assert response.status_code == 413


def test_readiness_does_not_confuse_live_process_with_loaded_model(client):
    from types import SimpleNamespace

    client.app.state.worker = SimpleNamespace(engine=SimpleNamespace(process=object(), ready=False))
    assert client.get("/ready").json()["engine_loaded"] is False
    client.app.state.worker.engine.ready = True
    assert client.get("/ready").json()["engine_loaded"] is True


def test_hosted_preview_origin_works_behind_tls_termination(config, repo, objects):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from voice_studio_service.api import create_app

    config.mode = "hosted"
    config.owner_password_hash = SecretStr(passwords.hash("a-strong-test-password"))
    config.session_secret = SecretStr("test-session-secret-at-least-thirty-two-characters")
    # The upstream HTTP request retains the public preview Host; TLS terminates at Daytona.
    with TestClient(
        create_app(config, repo, objects), base_url="http://8765-session.preview.example"
    ) as browser:
        response = browser.post(
            "/v1/auth/login",
            json={"password": "a-strong-test-password"},
            headers={"Origin": "https://8765-session.preview.example"},
        )
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
