import hashlib
import sys
from types import SimpleNamespace

import pytest
from filelock import FileLock
from voice_studio_core.contracts import DomainError
from voice_studio_engine import models


@pytest.fixture
def download(monkeypatch, tmp_path):
    source = tmp_path / "download"
    source.write_bytes(b"verified model fixture")
    spec = {"models": {"nano": {"revision": "pinned", "repo_id": "fixture",
            "files": [{"name": "weight", "size": source.stat().st_size,
                       "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}}}
    monkeypatch.setattr(models, "model_lock", lambda: spec)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        hf_hub_download=lambda **_: str(source)))
    return source


def test_cli_and_api_installations_share_lock(download, tmp_path):
    root = tmp_path / "models"
    directory = root / "nano"
    directory.mkdir(parents=True)
    with FileLock(str(directory / ".install.lock")):
        with pytest.raises(DomainError, match="already being installed"):
            models.install_model(root, "nano")
    models.install_model(root, "nano")
    assert models.verify_model(root, "nano")


def test_failed_download_preserves_prior_install(download, tmp_path):
    root = tmp_path / "models"
    target = models.install_model(root, "nano")
    (target / "weight").write_bytes(b"damaged old file")
    download.write_bytes(b"corrupt response")
    with pytest.raises(DomainError, match="integrity"):
        models.install_model(root, "nano")
    assert (target / "weight").read_bytes() == b"damaged old file"
    assert not list(target.parent.glob(".install-*"))


def test_interrupted_activation_recovers_verified_previous(download, tmp_path):
    root = tmp_path / "models"
    target = models.install_model(root, "nano")
    target.rename(target.with_name("pinned.old"))
    download.unlink()  # Recovery must not need the network/cache again.
    assert models.install_model(root, "nano") == target
    assert models.verify_model(root, "nano")
