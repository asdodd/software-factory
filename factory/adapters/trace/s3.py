"""S3 trace backend for an S3-compatible endpoint inside the boundary.

There is no default endpoint: endpoint_url is required, so nothing can silently
point at a public cloud. Credentials come from secret references; agents get a
store built with write-only credentials (put only), while grading,
self-improvement, and humans use separate read credentials. Objects are never
overwritten: puts use If-None-Match where the endpoint supports it, and bucket
lifecycle policy (not the factory) handles retention.

boto3 is imported lazily and only when this backend is selected. This build
never exercises it against a real endpoint; tests inject the in-process fake
(factory/adapters/fake/s3.py) through CLIENT_FACTORY."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from factory.adapters.trace.objects import ObjectTraceStore
from factory.config.schema import S3Config, S3Credentials
from factory.ports.context import AdapterContext
from factory.ports.errors import NotFound

CLIENT_FACTORY: Callable[..., object] | None = None
THROTTLE_CODES = {"SlowDown", "Throttling", "ThrottlingException", "RequestLimitExceeded", "503", "500",
                  "InternalError", "ServiceUnavailable"}


class S3ConfigError(ValueError):
    pass


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


def make_client(s3: S3Config, creds: Mapping[str, str]):
    if CLIENT_FACTORY is not None:
        return CLIENT_FACTORY(s3, creds)
    import boto3
    from botocore.config import Config

    verify: bool | str = s3.tls.ca_bundle_path or s3.tls.verify
    return boto3.client(
        "s3", endpoint_url=s3.endpoint_url, region_name=s3.region or None, verify=verify,
        aws_access_key_id=creds.get("access_key_id"), aws_secret_access_key=creds.get("secret_access_key"),
        aws_session_token=creds.get("session_token") or None,
        config=Config(s3={"addressing_style": s3.addressing}, retries={"max_attempts": 1, "mode": "standard"},
                      connect_timeout=10, read_timeout=60),
    )


class S3Backend:
    """ObjectBackend over an S3 client, with bounded retries on throttling."""

    def __init__(self, client, bucket: str, encryption: Mapping[str, str], max_retries: int = 4,
                 backoff_seconds: float = 0.2, sleep: Callable[[float], None] = time.sleep) -> None:
        self.client = client
        self.bucket = bucket
        self.encryption = dict(encryption)
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self.sleep = sleep

    def _call(self, fn, **kwargs):
        for attempt in range(self.max_retries + 1):
            try:
                return fn(Bucket=self.bucket, **kwargs)
            except Exception as exc:  # noqa: BLE001 - classified by error code below
                if _error_code(exc) in THROTTLE_CODES and attempt < self.max_retries:
                    self.sleep(self.backoff * (2 ** attempt))
                    continue
                raise
        raise AssertionError("unreachable")

    def put(self, key: str, data: bytes) -> None:
        try:
            self._call(self.client.put_object, Key=key, Body=data, IfNoneMatch="*", **self.encryption)
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) not in ("PreconditionFailed", "412"):
                raise  # 412: already uploaded by an earlier attempt; objects are immutable

    def get(self, key: str) -> bytes:
        try:
            return self._call(self.client.get_object, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) in ("NoSuchKey", "404"):
                raise NotFound(key) from None
            raise

    def exists(self, key: str) -> bool:
        try:
            self._call(self.client.head_object, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001
            # 404: absent. 403: write-only credentials cannot look; If-None-Match on put still protects the object.
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound", "403", "AccessDenied"):
                return False
            raise

    def list(self, prefix: str) -> list[str]:
        keys, token = [], None
        while True:
            kwargs = {"Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._call(self.client.list_objects_v2, **kwargs)
            keys.extend(o["Key"] for o in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return sorted(keys)
            token = page["NextContinuationToken"]


def encryption_args(s3: S3Config, required: bool) -> dict[str, str]:
    mode = s3.encryption.mode
    if not mode:
        if required:
            raise S3ConfigError("trace_store.s3.encryption.mode is required when require_encryption is true")
        return {}
    if mode == "sse-kms":
        if not s3.encryption.kms_key_id:
            raise S3ConfigError("trace_store.s3.encryption.kms_key_id is required for sse-kms")
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": s3.encryption.kms_key_id}
    return {"ServerSideEncryption": "AES256"}


def resolve_credentials(ctx: AdapterContext, creds: S3Credentials, label: str) -> dict[str, str]:
    out = {}
    for name in ("access_key_id", "secret_access_key", "session_token"):
        ref = getattr(creds, name)
        if ref.env or ref.file:
            out[name] = ctx.secrets.resolve(ref, f"trace_store.s3.{label}.{name}")
    return out


class S3TraceStore(ObjectTraceStore):
    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> S3TraceStore:
        ts = ctx.config.trace_store
        s3 = ts.s3
        if not s3.endpoint_url:
            raise S3ConfigError("trace_store.s3.endpoint_url is required (there is no default endpoint)")
        if not s3.bucket:
            raise S3ConfigError("trace_store.s3.bucket is required")
        enc = encryption_args(s3, ts.require_encryption)
        write = S3Backend(make_client(s3, resolve_credentials(ctx, s3.write_credentials, "write_credentials")),
                          s3.bucket, enc)
        read = S3Backend(make_client(s3, resolve_credentials(ctx, s3.read_credentials, "read_credentials")),
                         s3.bucket, enc)
        return cls(write, s3.prefix, s3.key_template, ctx.metrics, ts.capture_level,
                   ctx.base_dir / ts.local_fs.retry_queue, read_backend=read)
