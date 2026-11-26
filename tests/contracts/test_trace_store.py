"""TraceStore contract: run against every backend (memory, local_fs; S3 joins in Phase 4)."""

from datetime import UTC, datetime, timedelta

import pytest

from factory.adapters.fake.s3 import READ_ONLY, WRITE_ONLY, FakeS3
from factory.adapters.trace.objects import LocalFsBackend, MemoryBackend, ObjectTraceStore
from factory.adapters.trace.s3 import S3Backend
from factory.core.trace_writers import write_only
from factory.metrics import Metrics
from factory.ports.types import Artifact, Outcome, OutcomeRecord, RunMeta, TraceEvent, TraceQuery, Usage

TEMPLATE = "{prefix}{agent}/{yyyy}/{mm}/{dd}/{ticket_key}/{run_id}/"
T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def s3_store(tmp_path, fake=None, page_size=2):
    fake = fake or FakeS3()
    fake.bucket.page_size = page_size
    enc = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "key-1"}
    write = S3Backend(fake.client(WRITE_ONLY), "traces", enc, sleep=lambda s: None)
    read = S3Backend(fake.client(READ_ONLY), "traces", enc, sleep=lambda s: None)
    return ObjectTraceStore(write, "factory-traces/", TEMPLATE, Metrics(), "full", tmp_path / "retry", read_backend=read)


@pytest.fixture(params=["memory", "local_fs", "s3"])
def store(request, tmp_path):
    if request.param == "s3":
        return s3_store(tmp_path)
    backend = MemoryBackend() if request.param == "memory" else LocalFsBackend(tmp_path / "traces")
    return ObjectTraceStore(backend, "factory-traces/", TEMPLATE, Metrics(), "full", tmp_path / "retry")


def meta(run_id="run-1", agent="frontend-bot", ticket="SIM-1", when=T0, outcome=Outcome.MR_OPENED):
    return RunMeta(1, run_id, agent, ticket, "web/portal", {"implement": "p/m"}, "0.0.1", "abc", when,
                   when + timedelta(minutes=3), {"implement": 1.5}, Usage(10, 5), outcome)


def test_put_get_roundtrip_with_layout(store, tmp_path):
    art = tmp_path / "diff.patch"
    art.write_text("+x")
    store.put_run(meta(), [TraceEvent(T0, "model_call", {"stage": "implement"})], [Artifact("diff.patch", art, "text/x-diff")])
    store.flush()
    got, events, outcomes = store.get("run-1")
    assert got.ticket_key == "SIM-1" and got.outcome is Outcome.MR_OPENED and got.usage.total == 15
    assert [e.kind for e in events] == ["model_call"] and outcomes == ()
    keys = store.read_backend.list("factory-traces/frontend-bot/2026/03/02/SIM-1/run-1/")
    assert {k.rsplit("/", 1)[-1] for k in keys} >= {"meta.json", "trace.jsonl.gz", "diff.patch"}


def test_outcomes_are_new_objects_never_overwrites(store):
    store.put_run(meta(), [], [])
    store.flush()
    before = set(store.read_backend.list(""))
    store.append_outcome(OutcomeRecord("run-1", T0 + timedelta(days=1), "merged", {"by": "bob"}))
    store.append_outcome(OutcomeRecord("run-1", T0 + timedelta(days=2), "grade", {"score": 4}))
    store.flush()
    after = set(store.read_backend.list(""))
    assert before < after and len(after - before) == 2
    assert [o.kind for o in store.get("run-1")[2]] == ["merged", "grade"]
    key = sorted(before)[0]
    original = store.read_backend.get(key)
    try:
        store.backend.put(key, b"overwrite")
    except (FileExistsError, OSError):
        pass
    assert store.read_backend.get(key) == original


def test_list_filters_by_agent_date_ticket_outcome(store):
    store.put_run(meta("r1"), [], [])
    store.put_run(meta("r2", agent="docs-bot", ticket="SIM-2"), [], [])
    store.put_run(meta("r3", when=T0 + timedelta(days=3), outcome=Outcome.FAILED), [], [])
    store.flush()
    day = TraceQuery(since=T0, until=T0)
    assert sorted(m.run_id for m in store.list(day)) == ["r1", "r2"]
    assert [m.run_id for m in store.list(TraceQuery(agent="docs-bot", since=T0, until=T0))] == ["r2"]
    week = TraceQuery(since=T0, until=T0 + timedelta(days=7), outcome=Outcome.FAILED)
    assert [m.run_id for m in store.list(week)] == ["r3"]
    with pytest.raises(ValueError):
        list(store.list(TraceQuery(agent="docs-bot")))


def test_metadata_capture_stores_summary_only(tmp_path):
    s = ObjectTraceStore(MemoryBackend(), "", TEMPLATE, Metrics(), "metadata")
    s.put_run(meta(), [TraceEvent(T0, "model_call", {"prompt": "x"})], [])
    s.flush()
    assert not any(k.endswith("trace.jsonl.gz") for k in s.backend.list(""))


def test_upload_failure_queues_on_disk_and_retries(tmp_path):
    backend = MemoryBackend()
    metrics = Metrics()
    s = ObjectTraceStore(backend, "", TEMPLATE, metrics, "full", tmp_path / "retry")
    backend.fail_puts = 100
    s.put_run(meta(), [], [])
    s.flush()
    assert s.pending > 0 and metrics.count("trace_upload_failures") > 0 and backend.objects == {}
    revived = ObjectTraceStore(backend, "", TEMPLATE, metrics, "full", tmp_path / "retry")
    backend.fail_puts = 0
    revived.flush()
    assert revived.pending == 0 and revived.get("run-1")[0].run_id == "run-1"


def test_agent_writer_cannot_read_list_or_delete(store):
    writer = write_only(store)
    assert {n for n in dir(writer) if not n.startswith("_")} == {"put_run", "append_outcome", "flush"}
    with pytest.raises(AttributeError):
        writer.store = store


def test_outcome_from_write_only_credentials_needs_no_read(tmp_path):
    fake = FakeS3()
    agent = s3_store(tmp_path, fake)
    agent.put_run(meta(), [], [])
    agent.flush()
    later = s3_store(tmp_path / "later", fake)
    later.append_outcome(OutcomeRecord("run-1", T0 + timedelta(days=1), "mr_merged", {}, "frontend-bot", "SIM-1", T0))
    later.flush()
    assert [o.kind for o in later.get("run-1")[2]] == ["mr_merged"]


def test_s3_write_credentials_cannot_read_and_read_credentials_cannot_write(tmp_path):
    fake = FakeS3()
    store = s3_store(tmp_path, fake)
    store.put_run(meta(), [], [])
    store.flush()
    with pytest.raises(Exception, match="AccessDenied"):
        store.backend.get(next(iter(fake.bucket.objects)))
    with pytest.raises(Exception, match="AccessDenied"):
        store.backend.list("")
    with pytest.raises(Exception, match="AccessDenied"):
        store.read_backend.put("factory-traces/x", b"y")


def test_s3_encryption_is_sent_and_required(tmp_path):
    fake = FakeS3()
    store = s3_store(tmp_path, fake)
    store.put_run(meta(), [], [])
    store.flush()
    assert all(e["ServerSideEncryption"] == "aws:kms" and e["SSEKMSKeyId"] == "key-1"
               for e in fake.bucket.encryption.values())
    plain = S3Backend(fake.client(WRITE_ONLY), "traces", {}, sleep=lambda s: None)
    with pytest.raises(Exception, match="AccessDenied"):
        plain.put("factory-traces/unencrypted", b"x")


def test_s3_throttling_retries_and_partial_failure_queues(tmp_path):
    fake = FakeS3()
    store = s3_store(tmp_path, fake)
    fake.bucket.throttle_next = 3
    store.put_run(meta("r1"), [], [])
    store.flush()
    assert store.pending == 0
    fake.bucket.fail_next_puts = 2
    store.put_run(meta("r2"), [], [])
    store.flush()
    assert store.pending == 0, "transient 5xx errors are retried with backoff"
    fake.bucket.fail_next_puts = 1000
    store.put_run(meta("r3"), [], [])
    store.flush()
    assert store.pending == 3 and store.metrics.count("trace_upload_failures") == 3  # meta, trace, manifest
    fake.bucket.fail_next_puts = 0
    store.flush()
    assert store.pending == 0 and store.get("r3")[0].run_id == "r3"


def test_s3_backend_refuses_missing_endpoint_or_encryption(tmp_path):
    from factory.adapters.trace.s3 import S3ConfigError, encryption_args
    from factory.config.schema import S3Config, S3Encryption
    with pytest.raises(S3ConfigError, match="encryption.mode is required"):
        encryption_args(S3Config(encryption=S3Encryption(mode="")), required=True)
    assert encryption_args(S3Config(encryption=S3Encryption(mode="sse-s3")), required=True) == {
        "ServerSideEncryption": "AES256"}
