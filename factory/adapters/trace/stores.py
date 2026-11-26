from __future__ import annotations

from collections.abc import Mapping

from factory.adapters.trace.objects import LocalFsBackend, MemoryBackend, ObjectTraceStore
from factory.ports.context import AdapterContext


class MemoryTraceStore(ObjectTraceStore):
    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> MemoryTraceStore:
        ts = ctx.config.trace_store
        return cls(MemoryBackend(), "", ts.s3.key_template, ctx.metrics, ts.capture_level)


class LocalFsTraceStore(ObjectTraceStore):
    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> LocalFsTraceStore:
        ts = ctx.config.trace_store
        backend = LocalFsBackend(ctx.base_dir / ts.local_fs.root)
        return cls(backend, "", ts.s3.key_template, ctx.metrics, ts.capture_level, ctx.base_dir / ts.local_fs.retry_queue)
