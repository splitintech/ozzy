import hashlib

import pytest
from moto import mock_aws
from pydantic import ValidationError
from voice_studio_core.contracts import DomainError, SynthesisOptions
from voice_studio_core.storage import S3Storage, safe_key
from voice_studio_engine.models import verify_file
from voice_studio_engine.text import segments


@pytest.mark.parametrize("key", ["../private", "/absolute", "a/../../b", "a\\b", ".secret"])
def test_storage_traversal_rejected(key):
    with pytest.raises(DomainError):
        safe_key(key)


def test_symlink_cannot_escape_local_storage(objects, tmp_path):
    (objects.root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError):
        objects.read("escape/private")


def test_concurrent_prefix_deletion_is_idempotent(objects):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    root = objects.path("expiring/jobs/job-one")
    for attempt in range(30):
        directory = root / str(attempt)
        directory.mkdir(parents=True)
        (directory / "result").write_bytes(b"private audio fixture")
    barrier = threading.Barrier(8)

    def remove(_):
        barrier.wait(timeout=10)
        objects.delete_prefix("expiring/jobs/job-one")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(remove, range(8)))
    assert not root.exists()
    objects.delete_prefix("expiring/jobs/job-one")


def test_prefix_deletion_does_not_hide_permission_failure(objects, monkeypatch):
    import shutil

    objects.path("private").mkdir()

    def denied(path, onerror):
        error = PermissionError("Cannot delete this object")
        onerror(None, path, (type(error), error, None))

    monkeypatch.setattr(shutil, "rmtree", denied)
    with pytest.raises(PermissionError):
        objects.delete_prefix("private")


def test_unsupported_model_controls_rejected():
    for name in ["nano", "turbo"]:
        with pytest.raises(ValidationError):
            SynthesisOptions(model=name, cfg_weight=0.5)
    assert SynthesisOptions(model="original", cfg_weight=0.5).cfg_weight == 0.5


def test_segmentation_preserves_every_word():
    text = (
        "Hello, Dr. Rivera. Call 212-555-0199 before October 31. "
        + "This message must remain complete. " * 30
    )
    chunks = segments(text)
    assert " ".join(chunks).split() == text.split()
    assert all(len(chunk) <= 180 for chunk in chunks)


def test_model_file_verification_detects_corruption(tmp_path):
    file = tmp_path / "weight"
    file.write_bytes(b"abcdef")
    spec = {"size": 6, "sha256": hashlib.sha256(b"abcdef").hexdigest()}
    assert verify_file(file, spec)
    file.write_bytes(b"badbad")
    assert not verify_file(file, spec)


@mock_aws
def test_s3_storage_and_lifecycle(config, tmp_path):
    from pydantic import SecretStr

    config.s3_bucket = "voice-studio-test"
    config.s3_access_key = SecretStr("testing")
    config.s3_secret_key = SecretStr("testing")
    store = S3Storage(config)
    store.client.create_bucket(Bucket=config.s3_bucket)
    source = tmp_path / "script.json"
    source.write_text('{"text":"private"}')
    receipt = store.put("expiring/jobs/one/script.json", source, "application/json")
    assert receipt["size"] == source.stat().st_size
    assert store.read("expiring/jobs/one/script.json") == source.read_bytes()
    store.configure_retention(30)
    rules = store.client.get_bucket_lifecycle_configuration(Bucket=config.s3_bucket)["Rules"]
    assert rules[0]["Expiration"]["Days"] == 30
    store.delete_prefix("expiring/jobs/one")
    assert store.client.list_objects_v2(Bucket=config.s3_bucket).get("KeyCount", 0) == 0


@mock_aws
def test_delete_purges_private_reference_versions(config, tmp_path):
    from pydantic import SecretStr
    config.s3_bucket = "voice-studio-versions"
    config.s3_access_key = SecretStr("testing")
    config.s3_secret_key = SecretStr("testing")
    store = S3Storage(config)
    store.client.create_bucket(Bucket=config.s3_bucket)
    store.client.put_bucket_versioning(Bucket=config.s3_bucket, VersioningConfiguration={"Status": "Enabled"})
    source = tmp_path / "reference"
    source.write_bytes(b"private reference")
    store.put("voices/version-one/reference.wav", source)
    source.write_bytes(b"second version")
    store.put("voices/version-one/reference.wav", source)
    store.delete_prefix("voices/version-one")
    result = store.client.list_object_versions(Bucket=config.s3_bucket)
    assert not result.get("Versions") and not result.get("DeleteMarkers")
