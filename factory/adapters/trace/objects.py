"""Trace store over a minimal object interface (put/get/list), shared by the
memory, local_fs, and (Phase 4) s3 backends.

Layout under the key template, per run:
  trace.jsonl.gz, meta.json, artifacts/<name>, outcomes/<timestamp>-<n>.json
plus one manifest object per run under {prefix}manifests/<agent>/<yyyy-mm-dd>/<run_id>.json
so readers list a day, never the whole store. Objects are never overwritten.
Upload failures queue for retry and never raise into the pipeline."""

from __future__ import annotations

import dataclasses
import enum
import gzip
import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from factory.metrics import Metrics
from factory.ports.errors import NotFound
from factory.ports.types import Artifact, Outcome, OutcomeRecord, RunMeta, TraceEvent, TraceQuery, Usage

SCHEMA_VERSION = 1


class ObjectBackend(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> list[str]: ...
    def exists(self, key: str) -> bool: ...


def _jsonable(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, enum.Enum):
        return obj.value
    if dataclasses.is_dataclass(obj):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def meta_from_dict(d: Mapping) -> RunMeta:
    return RunMeta(
        schema_version=d["schema_version"], run_id=d["run_id"], agent=d["agent"], ticket_key=d["ticket_key"],
        repo=d["repo"], models=d["models"], factory_version=d["factory_version"], config_hash=d["config_hash"],
        started=datetime.fromisoformat(d["started"]),
        finished=datetime.fromisoformat(d["finished"]) if d.get("finished") else None,
        stage_seconds=d["stage_seconds"], usage=Usage(**d["usage"]),
        outcome=Outcome(d["outcome"]) if d.get("outcome") else None,
    )


class ObjectTraceStore:
    def __init__(self, backend: ObjectBackend, prefix: str, key_template: str, metrics: Metrics,
                 capture_level: str = "full", retry_dir: Path | None = None) -> None:
        self.backend = backend
        self.prefix = prefix
        self.key_template = key_template
        self.metrics = metrics
        self.capture_level = capture_level
        self.retry_dir = retry_dir
        self._lock = threading.Lock()
        self._pending: list[tuple[str, bytes]] = []
        self._outcome_seq = 0
        self._run_prefixes: dict[str, str] = {}
        self._load_retry_queue()

    # ---- layout -----------------------------------------------------
    def run_prefix(self, meta: RunMeta) -> str:
        t = meta.started
        return self.key_template.format(prefix=self.prefix, agent=meta.agent, yyyy=f"{t.year:04d}", mm=f"{t.month:02d}",
                                        dd=f"{t.day:02d}", ticket_key=meta.ticket_key, run_id=meta.run_id)

    def _manifest_key(self, agent: str, day: datetime, run_id: str) -> str:
        return f"{self.prefix}manifests/{agent}/{day:%Y-%m-%d}/{run_id}.json"

    # ---- writer -------------------------------------------------------
    def put_run(self, meta: RunMeta, events: Sequence[TraceEvent], artifacts: Sequence[Artifact]) -> None:
        base = self.run_prefix(meta)
        meta_doc = {**_jsonable(meta), "schema_version": SCHEMA_VERSION, "capture_level": self.capture_level}
        objects = [(f"{base}meta.json", json.dumps(meta_doc, sort_keys=True).encode())]
        if self.capture_level == "full":
            lines = "".join(json.dumps(_jsonable(e), sort_keys=True) + "\n" for e in events)
            objects.append((f"{base}trace.jsonl.gz", gzip.compress(lines.encode(), mtime=0)))
            for artifact in artifacts:
                try:
                    objects.append((f"{base}artifacts/{artifact.name}", Path(artifact.path).read_bytes()))
                except OSError:
                    self.metrics.incr("trace_artifact_missing")
        manifest = {"run_id": meta.run_id, "prefix": base, "agent": meta.agent, "ticket_key": meta.ticket_key,
                    "outcome": _jsonable(meta.outcome), "started": meta.started.isoformat()}
        objects.append((self._manifest_key(meta.agent, meta.started, meta.run_id), json.dumps(manifest).encode()))
        with self._lock:
            self._run_prefixes[meta.run_id] = base
            self._pending.extend(objects)

    def append_outcome(self, record: OutcomeRecord) -> None:
        with self._lock:
            base = self._run_prefixes.get(record.run_id)
            self._outcome_seq += 1
            seq = self._outcome_seq
        if base is None:
            base = self._find_prefix(record.run_id)
        key = f"{base}outcomes/{record.at:%Y%m%dT%H%M%S}-{seq:04d}-{record.kind}.json"
        with self._lock:
            self._pending.append((key, json.dumps(_jsonable(record), sort_keys=True).encode()))

    def flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, []
        failed = []
        for key, data in batch:
            try:
                if not self.backend.exists(key):
                    self.backend.put(key, data)
            except Exception:  # noqa: BLE001 - upload failures must never fail a ticket
                failed.append((key, data))
                self.metrics.incr("trace_upload_failures")
        with self._lock:
            self._pending = failed + self._pending
        self._save_retry_queue()

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def _save_retry_queue(self) -> None:
        if self.retry_dir is None:
            return
        self.retry_dir.mkdir(parents=True, exist_ok=True)
        for old in self.retry_dir.glob("*.json"):
            old.unlink()
        with self._lock:
            pending = list(self._pending)
        for i, (key, data) in enumerate(pending):
            (self.retry_dir / f"{i:06d}.json").write_text(json.dumps({"key": key, "data": data.hex()}))

    def _load_retry_queue(self) -> None:
        if self.retry_dir is None or not self.retry_dir.is_dir():
            return
        for path in sorted(self.retry_dir.glob("*.json")):
            item = json.loads(path.read_text())
            self._pending.append((item["key"], bytes.fromhex(item["data"])))

    # ---- reader ---------------------------------------------------------
    def _find_prefix(self, run_id: str) -> str:
        for key in self.backend.list(f"{self.prefix}manifests/"):
            if key.endswith(f"/{run_id}.json"):
                return json.loads(self.backend.get(key))["prefix"]
        raise NotFound(f"run {run_id}")

    def get(self, run_id: str):
        base = self._find_prefix(run_id)
        meta = meta_from_dict(json.loads(self.backend.get(f"{base}meta.json")))
        events: tuple[TraceEvent, ...] = ()
        if self.backend.exists(f"{base}trace.jsonl.gz"):
            raw = gzip.decompress(self.backend.get(f"{base}trace.jsonl.gz")).decode()
            events = tuple(
                TraceEvent(datetime.fromisoformat(d["at"]), d["kind"], d["payload"])
                for d in (json.loads(line) for line in raw.splitlines() if line)
            )
        outcomes = tuple(
            OutcomeRecord(d["run_id"], datetime.fromisoformat(d["at"]), d["kind"], d["payload"])
            for d in (json.loads(self.backend.get(k)) for k in sorted(self.backend.list(f"{base}outcomes/")))
        )
        return meta, events, outcomes

    def list(self, query: TraceQuery) -> Iterator[RunMeta]:
        if query.since is None or query.until is None:
            raise ValueError("list needs a since/until date range so it never scans the whole store")
        agents = [query.agent] if query.agent else sorted({
            k[len(f"{self.prefix}manifests/"):].split("/", 1)[0] for k in self.backend.list(f"{self.prefix}manifests/")
        })
        day = query.since.date()
        while day <= query.until.date():
            for agent in agents:
                for key in sorted(self.backend.list(f"{self.prefix}manifests/{agent}/{day:%Y-%m-%d}/")):
                    entry = json.loads(self.backend.get(key))
                    if query.ticket_key and entry["ticket_key"] != query.ticket_key:
                        continue
                    if query.outcome and entry["outcome"] != query.outcome.value:
                        continue
                    yield meta_from_dict(json.loads(self.backend.get(f"{entry['prefix']}meta.json")))
            day += timedelta(days=1)


class MemoryBackend:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_puts = 0

    def put(self, key: str, data: bytes) -> None:
        if self.fail_puts:
            self.fail_puts -= 1
            raise OSError("simulated upload failure")
        if key in self.objects:
            raise FileExistsError(key)
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise NotFound(key) from None

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def exists(self, key: str) -> bool:
        return key in self.objects


class LocalFsBackend:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"key escapes the trace root: {key}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as fh:
            fh.write(data)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise NotFound(key) from None

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        parent = base if (not prefix or prefix.endswith("/")) else base.parent
        if not parent.is_dir():
            return []
        return sorted(str(p.relative_to(self.root)) for p in parent.rglob("*")
                      if p.is_file() and str(p.relative_to(self.root)).startswith(prefix))

    def exists(self, key: str) -> bool:
        return self._path(key).exists()
