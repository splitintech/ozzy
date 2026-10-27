import hashlib
import json
import os
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from voice_studio_core.contracts import DomainError
from voice_studio_core.storage import sha256


def model_lock():
    return json.loads(files("voice_studio_core").joinpath("model-lock.json").read_text())


def model_path(root, name):
    spec = model_lock()["models"][name]
    return Path(root) / name / spec["revision"]


def verify_file(path, spec):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size != spec["size"]:
        return False
    if spec.get("sha256"):
        return sha256(path) == spec["sha256"]
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == spec["git_blob"]


def verify_model(root, name, deep=True):
    directory = model_path(root, name)
    spec = model_lock()["models"][name]
    if not (directory / ".verified.json").exists():
        return False
    return all(
        verify_file(directory / item["name"], item)
        if deep
        else (directory / item["name"]).is_file()
        and (directory / item["name"]).stat().st_size == item["size"]
        for item in spec["files"]
    )


def install_model(root, name, progress=None):
    if name not in model_lock()["models"]:
        raise DomainError("unknown_model", "Choose original, turbo, or nano", 422)
    from filelock import FileLock, Timeout

    target = model_path(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    # CLI and API installations share this OS lock, including across processes.
    # A crashed installer releases it automatically; the next run can recover.
    try:
        with FileLock(str(target.parent / ".install.lock"), timeout=0):
            return _install_model(root, name, progress)
    except Timeout:
        raise DomainError("install_active", "This model is already being installed", 409) from None


def _install_model(root, name, progress=None):
    os.environ.setdefault("HF_HOME", str(Path(root).resolve() / ".download-cache"))
    from huggingface_hub import hf_hub_download

    spec = model_lock()["models"][name]
    target = model_path(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    old = target.with_name(target.name + ".old")
    if old.exists() and not target.exists():
        os.replace(old, target)
    if verify_model(root, name):
        return target
    required = 2 * sum(item["size"] for item in spec["files"]) + 512 * 1024 * 1024
    if shutil.disk_usage(target.parent).free < required:
        raise DomainError(
            "disk_space",
            f"Free at least {required / 1e9:.1f} GB for model download and installation",
            409,
        )
    # Downloads are resumable in the HF cache. Only verified files become active.
    with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as temporary:
        stage = Path(temporary)
        for index, item in enumerate(spec["files"]):
            if progress:
                progress(index, len(spec["files"]), item["name"])
            cached = hf_hub_download(
                repo_id=spec["repo_id"],
                revision=spec["revision"],
                filename=item["name"],
                cache_dir=str(Path(root).resolve() / ".download-cache" / "hub"),
            )
            copied = stage / item["name"]
            shutil.copyfile(cached, copied)
            if not verify_file(copied, item):
                raise DomainError(
                    "model_integrity", "Downloaded model failed integrity verification", 422
                )
        (stage / ".verified.json").write_text(
            json.dumps(
                {
                    "revision": spec["revision"],
                    "files": [f["name"] for f in spec["files"]],
                }
            )
        )
        if old.exists():
            shutil.rmtree(old)
        if target.exists():
            os.replace(target, old)
        try:
            os.replace(stage, target)
        except BaseException:
            if old.exists():
                os.replace(old, target)
            raise
        if old.exists():
            shutil.rmtree(old)
    return target
