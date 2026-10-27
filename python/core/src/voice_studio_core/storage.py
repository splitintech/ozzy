import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

from .contracts import DomainError


def safe_key(key):
    path = PurePosixPath(key)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in key
        or not path.parts
        or any(part.startswith(".") for part in path.parts)
    ):
        raise DomainError("invalid_key", "Invalid storage object", 400)
    return str(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class LocalStorage:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key):
        path = (self.root / safe_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise DomainError("invalid_key", "Invalid storage object")
        return path

    def put(self, key, source, content_type="application/octet-stream"):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as out, Path(source).open("rb") as inp:
                for block in iter(lambda: inp.read(1024 * 1024), b""):
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"size": target.stat().st_size, "checksum": sha256(target)}

    def download(self, key, destination):
        import shutil

        shutil.copyfile(self.path(key), destination)

    def read(self, key):
        return self.path(key).read_bytes()

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix):
        import shutil

        def tolerate_removed(_, __, error):
            # Python 3.11 raises when another cleanup removes a visited child.
            # Missing objects already satisfy deletion; permission/I/O failures do not.
            if not isinstance(error[1], FileNotFoundError):
                raise error[1]

        target = self.path(prefix)
        if target.is_dir():
            shutil.rmtree(target, onerror=tolerate_removed)


class S3Storage:
    def __init__(self, settings):
        import boto3

        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint or None,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key.get_secret_value() or None,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value() or None,
        )

    def put(self, key, source, content_type="application/octet-stream"):
        key = safe_key(key)
        checksum = sha256(source)
        self.client.upload_file(
            str(source),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": {"sha256": checksum}},
        )
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        if head["ContentLength"] != Path(source).stat().st_size:
            raise OSError("Object verification failed")
        if head.get("Metadata", {}).get("sha256") != checksum:
            raise OSError("Object checksum metadata mismatch")
        return {"size": head["ContentLength"], "checksum": checksum}

    def download(self, key, destination):
        self.client.download_file(self.bucket, safe_key(key), str(destination))

    def read(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=safe_key(key))
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=safe_key(key))

    def delete_prefix(self, prefix):
        prefix = safe_key(prefix).rstrip("/") + "/"
        versioning = self.client.get_bucket_versioning(Bucket=self.bucket).get("Status")
        if versioning in {"Enabled", "Suspended"}:
            pages = self.client.get_paginator("list_object_versions").paginate(Bucket=self.bucket, Prefix=prefix)
            for page in pages:
                entries = [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
                for offset in range(0, len(entries), 1000):
                    self._delete_objects([{"Key": obj["Key"], "VersionId": obj["VersionId"]}
                                          for obj in entries[offset:offset + 1000]])
        else:
            pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
            for page in pages:
                self._delete_objects([{"Key": obj["Key"]} for obj in page.get("Contents", [])])

    def _delete_objects(self, objects):
        if objects:
            result = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects, "Quiet": True})
            if result.get("Errors"):
                raise OSError("Some storage objects could not be deleted")

    def configure_retention(self, days):
        # Dedicated bucket required: merge our rule, preserving unrelated rules.
        try:
            rules = self.client.get_bucket_lifecycle_configuration(
                Bucket=self.bucket,
            )["Rules"]
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
                raise
            rules = []
        rules = [rule for rule in rules if rule.get("ID") != "voice-studio-expiry"]
        rules.append(
            {
                "ID": "voice-studio-expiry",
                "Status": "Enabled",
                "Filter": {"Prefix": "expiring/"},
                "Expiration": {"Days": days},
                "NoncurrentVersionExpiration": {"NoncurrentDays": days},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
        )
        self.client.put_bucket_lifecycle_configuration(
            Bucket=self.bucket,
            LifecycleConfiguration={"Rules": rules},
        )


def storage_for(settings):
    if settings.storage_backend == "daytona_volume":
        from .volume_storage import VolumeStorage

        return VolumeStorage(settings.volume_dir)
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings.data_dir / "objects")
