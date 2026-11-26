"""In-process fake S3 implementing the operations the trace backend uses:
put_object, get_object, head_object, list_objects_v2 (with prefix and
pagination). It models what matters for the factory:

- credentials are write-only or read-only (AccessDenied otherwise),
- a bucket policy can require server-side encryption,
- If-None-Match: * refuses overwrites (PreconditionFailed),
- injected throttling (SlowDown) and partial failures (InternalError)."""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass, field

from botocore.exceptions import ClientError


def _err(code: str, op: str, status: int) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, op)


@dataclass
class FakeBucket:
    objects: dict[str, bytes] = field(default_factory=dict)
    encryption: dict[str, dict[str, str]] = field(default_factory=dict)
    require_encryption: bool = True
    page_size: int = 1000
    throttle_next: int = 0
    fail_next_puts: int = 0
    calls: list[tuple[str, str]] = field(default_factory=list)


class FakeS3Client:
    """permissions: subset of {"put", "get", "head", "list"}."""

    def __init__(self, bucket_name: str, bucket: FakeBucket, permissions: set[str]) -> None:
        self.bucket_name = bucket_name
        self.bucket = bucket
        self.permissions = permissions

    def _check(self, op: str, bucket: str, perm: str) -> None:
        self.bucket.calls.append((op, perm))
        if bucket != self.bucket_name:
            raise _err("NoSuchBucket", op, 404)
        if self.bucket.throttle_next:
            self.bucket.throttle_next -= 1
            raise _err("SlowDown", op, 503)
        if perm not in self.permissions:
            raise _err("AccessDenied" if perm != "head" else "403", op, 403)

    def put_object(self, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str = "",  # noqa: N803
                   ServerSideEncryption: str = "", SSEKMSKeyId: str = "") -> Mapping:  # noqa: N803
        self._check("PutObject", Bucket, "put")
        if self.bucket.fail_next_puts:
            self.bucket.fail_next_puts -= 1
            raise _err("InternalError", "PutObject", 500)
        if self.bucket.require_encryption and not ServerSideEncryption:
            raise _err("AccessDenied", "PutObject", 403)
        if IfNoneMatch == "*" and Key in self.bucket.objects:
            raise _err("PreconditionFailed", "PutObject", 412)
        self.bucket.objects[Key] = bytes(Body)
        self.bucket.encryption[Key] = {"ServerSideEncryption": ServerSideEncryption, "SSEKMSKeyId": SSEKMSKeyId}
        return {"ETag": f'"{len(Body)}"'}

    def get_object(self, Bucket: str, Key: str) -> Mapping:  # noqa: N803
        self._check("GetObject", Bucket, "get")
        if Key not in self.bucket.objects:
            raise _err("NoSuchKey", "GetObject", 404)
        return {"Body": io.BytesIO(self.bucket.objects[Key])}

    def head_object(self, Bucket: str, Key: str) -> Mapping:  # noqa: N803
        self._check("HeadObject", Bucket, "head")
        if Key not in self.bucket.objects:
            raise _err("404", "HeadObject", 404)
        return {"ContentLength": len(self.bucket.objects[Key])}

    def list_objects_v2(self, Bucket: str, Prefix: str = "", MaxKeys: int = 1000,  # noqa: N803
                        ContinuationToken: str = "") -> Mapping:  # noqa: N803
        self._check("ListObjectsV2", Bucket, "list")
        keys = sorted(k for k in self.bucket.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        size = min(MaxKeys, self.bucket.page_size)
        page = keys[start:start + size]
        truncated = start + size < len(keys)
        out = {"Contents": [{"Key": k, "Size": len(self.bucket.objects[k])} for k in page], "IsTruncated": truncated}
        if truncated:
            out["NextContinuationToken"] = str(start + size)
        return out


WRITE_ONLY = {"put"}
READ_ONLY = {"get", "head", "list"}


class FakeS3:
    """Hands out clients by credential: keys starting "write" are write-only, "read" read-only."""

    def __init__(self, bucket_name: str = "traces", require_encryption: bool = True) -> None:
        self.bucket_name = bucket_name
        self.bucket = FakeBucket(require_encryption=require_encryption)

    def client(self, permissions: set[str]) -> FakeS3Client:
        return FakeS3Client(self.bucket_name, self.bucket, permissions)

    def factory(self, s3_config, creds: Mapping[str, str]) -> FakeS3Client:
        key = creds.get("access_key_id", "")
        perms = WRITE_ONLY if key.startswith("write") else READ_ONLY if key.startswith("read") else set()
        return self.client(perms)
