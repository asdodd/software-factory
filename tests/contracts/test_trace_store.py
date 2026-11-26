"""TraceStore contract: run against every backend (memory, local_fs; S3 joins in Phase 4)."""

from datetime import UTC, datetime, timedelta

import pytest

from factory.adapters.trace.objects import LocalFsBackend, MemoryBackend, ObjectTraceStore
from factory.core.trace_writers import write_only
from factory.metrics import Metrics
from factory.ports.types import Artifact, Outcome, OutcomeRecord, RunMeta, TraceEvent, TraceQuery, Usage

TEMPLATE = "{prefix}{agent}/{yyyy}/{mm}/{dd}/{ticket_key}/{run_id}/"
T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "local_fs"])
def store(request, tmp_path):
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
    keys = store.backend.list("factory-traces/frontend-bot/2026/03/02/SIM-1/run-1/")
    assert {k.rsplit("/", 1)[-1] for k in keys} >= {"meta.json", "trace.jsonl.gz", "diff.patch"}


def test_outcomes_are_new_objects_never_overwrites(store):
    store.put_run(meta(), [], [])
    store.flush()
    before = set(store.backend.list(""))
    store.append_outcome(OutcomeRecord("run-1", T0 + timedelta(days=1), "merged", {"by": "bob"}))
    store.append_outcome(OutcomeRecord("run-1", T0 + timedelta(days=2), "grade", {"score": 4}))
    store.flush()
    after = set(store.backend.list(""))
    assert before < after and len(after - before) == 2
    assert [o.kind for o in store.get("run-1")[2]] == ["merged", "grade"]
    with pytest.raises((FileExistsError, OSError)):
        store.backend.put(sorted(before)[0], b"overwrite")


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
