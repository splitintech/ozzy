"""Provider-local cold wakes preserve secrets without exposing them to the app."""
import base64
import json
import os

import pytest
from voice_studio_core.contracts import DomainError
from voice_studio_service.bootstrap import load_bootstrap

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Hosted supervisor requires Unix ownership and permissions")


def incoming(tmp_path, *, enabled=True):
    ram = tmp_path / "ram" / "config.json"
    ram.parent.mkdir(mode=0o700)
    payload = {
        "api_key": "narrow-lifecycle-key",
        "owner_password_hash": "owner-hash",
        "session_secret": "s" * 40,
        "backup_key": base64.b64encode(b"x" * 32).decode(),
        "deployment": {"on_demand_bootstrap": enabled, "initialized": True},
        "sandbox_id": "sandbox-one",
        "unneeded_owner_secret": "must-not-persist",
    }
    ram.write_text(json.dumps(payload))
    ram.chmod(0o600)
    return ram, tmp_path / "private" / "bootstrap.json", payload


def test_opted_in_cold_wake_needs_no_ram_or_owner_vault(tmp_path):
    ram, persistent, payload = incoming(tmp_path)
    first = load_bootstrap(ram, persistent)
    assert not ram.exists()
    assert persistent.stat().st_mode & 0o777 == 0o600
    assert persistent.parent.stat().st_mode & 0o777 == 0o700
    assert persistent.stat().st_uid == os.geteuid()
    assert load_bootstrap(ram, persistent) == first
    assert first["backup_key"] == payload["backup_key"]
    assert first["deployment"]["initialized"] is True
    assert "unneeded_owner_secret" not in first


def test_legacy_bootstrap_stays_ephemeral(tmp_path):
    ram, persistent, _ = incoming(tmp_path, enabled=False)
    load_bootstrap(ram, persistent)
    assert not ram.exists() and not persistent.exists()
    with pytest.raises(DomainError, match="Private runtime startup"):
        load_bootstrap(ram, persistent)


def test_opt_in_requires_an_already_initialized_deployment(tmp_path):
    ram, persistent, payload = incoming(tmp_path)
    payload["deployment"]["initialized"] = False
    ram.write_text(json.dumps(payload))
    with pytest.raises(DomainError):
        load_bootstrap(ram, persistent)
    assert ram.exists() and not persistent.exists()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666])
def test_public_or_group_readable_material_is_rejected(tmp_path, mode):
    ram, persistent, _ = incoming(tmp_path)
    ram.chmod(mode)
    with pytest.raises(DomainError):
        load_bootstrap(ram, persistent)
    assert not persistent.exists()


def test_symlink_and_wrong_sandbox_cannot_supply_bootstrap(tmp_path, monkeypatch):
    ram, persistent, _ = incoming(tmp_path)
    real = ram.with_name("real.json")
    ram.rename(real)
    ram.symlink_to(real)
    with pytest.raises(DomainError):
        load_bootstrap(ram, persistent)
    ram.unlink()
    real.rename(ram)
    monkeypatch.setenv("DAYTONA_SANDBOX_ID", "another-sandbox")
    with pytest.raises(DomainError):
        load_bootstrap(ram, persistent)
    assert not persistent.exists()


def test_failed_rotation_preserves_previous_provider_material(tmp_path):
    ram, persistent, _ = incoming(tmp_path)
    original = load_bootstrap(ram, persistent)
    ram.write_text("invalid-private-material")
    ram.chmod(0o600)
    with pytest.raises(DomainError) as failure:
        load_bootstrap(ram, persistent)
    assert "invalid-private-material" not in str(failure.value)
    assert json.loads(persistent.read_text()) == original
