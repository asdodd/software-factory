# Phase 4 Report: Monitoring, Metrics, S3 Traces, Grading, Self-Improvement

Branch: `claude/software-factory-phase3` (I continued on the branch you approved, since you were offline). You told me to keep working, so I treated that as the go-ahead for Phase 4.

Status: complete. Everything runs against fakes. The S3 backend was tested only against the in-process fake S3 and never against a real endpoint.

## How to check it

```sh
python3 -m pytest -q                          # 184 tests + 46 subtests, network guard on
factory metrics --days 7 [--format prometheus]
factory grade --enable feedback.grading [--calibrate labels.jsonl]
factory improve --enable feedback.grading --enable feedback.self_improvement
factory scenario run tests/scenarios/cases/self_improvement_proposal.toml
```

## What works

Each of the following is behind its own feature toggle, in the spec's order.

**Monitoring intake** (`monitoring` plus `intake.monitoring`):
- Findings from the `Monitor` port go through the same intake service as everything else, so they get idempotency on the finding id, fingerprint dedupe, readiness, routing and override tracking.
- Findings below `intake.monitoring.min_severity` are skipped and counted.
- Monitors are trusted sources, but `intake.monitoring.auto_label` defaults to off (draft-only).
- A finding's acceptance criteria are "the finding no longer reproduces", and its verify step is "re-run the monitor".
- A new adapter, `monitor.command`, runs any regression suite, log scanner or probe that prints JSON-lines findings. It uses a fixed argv, a scrubbed environment and a timeout.

**Metrics** (`feedback.metrics`; `factory metrics`, JSON or Prometheus text). These are computed deterministically from traces, using read credentials, plus the state store:
- cost per ticket;
- MR acceptance rate: the agent now writes a `mr_merged` or `mr_closed` outcome object next to the run when it stops watching an MR;
- review rounds;
- human override rate at intake: durable counters, by source;
- mean time per stage;
- tool-call success rate.

**S3 trace backend** (`trace_store.backend = "s3"`):
- Uses boto3, imported only when selected (pinned in an optional extra: `pip install .[s3]`).
- `endpoint_url` is required, with no implicit default. TLS verification and a CA bundle are configurable, as are path or virtual addressing and SSE-KMS or SSE-S3. `require_encryption` refuses to start without an encryption mode.
- **Separate credentials.** Agents' store is built with write credentials and only ever puts. Outcomes are located from the run's identity, so writing them never needs a read. Readers (metrics, grading, self-improvement) use separate read credentials.
- Objects are immutable: `If-None-Match: *`. A retry that finds the object already there counts as uploaded.
- Throttling and 5xx errors are retried with backoff. Failures beyond the retries queue on disk and never fail a ticket.
- Redaction runs before upload, as before.
- The same `TraceStore` contract suite now runs against memory, local_fs and S3 on the fake.
- **The fake S3** (`factory/adapters/fake/s3.py`) models put, get, head and list with pagination, write-only versus read-only credentials, a bucket policy requiring encryption, overwrite refusal, throttling and partial failures.

**Grading** (`feedback.grading`; `factory grade`):
- A cheaper model (the `grading` stage) scores each recent ungraded run on verbosity (1-5), wasted tool calls and excess tests.
- It scores from a deterministic trace summary, redacted, and the grade is stored as a new outcome object.
- **Calibration:** `factory grade --calibrate labels.jsonl` grades human-labeled runs and measures agreement, where every score must be within `feedback.grading.tolerance`.
- Grades are marked `calibrated: true` only while the current grading model's agreement meets `min_agreement` (0.8).

**Self-improvement** (`feedback.self_improvement`; `factory improve`):
- It refuses to run without a calibrated grader.
- It reads the metrics, the calibrated grades, the worst runs and the editable files from the factory repo (`feedback.self_improvement.repo`), then asks the `improve` stage for proposals.
- Edits outside `allowed_paths` (default: `factory/skills/**` and `config/agents/**`), touching restricted paths, or carrying restricted content are dropped and listed in the MR.
- The result is opened as an MR on `ai/self-improvement/<date>` with the required template sections plus a **Retro** section of suggested automated checks and coding-standard updates.
- It never applies anything. In a scenario it proposes `access_policy.enabled = false` in `config/base.toml`; that edit is dropped and `main` is untouched.

## Test results

- 184 tests and 46 subtests pass; ruff reports no issues.
- 95 scenarios, 9 of them new:
  - monitoring files tickets with dedupe and the severity floor;
  - a monitoring finding through a routed ticket to an MR;
  - the metrics report;
  - tool-call success rate;
  - grading from an uncalibrated grader;
  - calibration failing;
  - a self-improvement proposal with dropped edits and a retro;
  - self-improvement requiring a repo;
  - S3 end to end with encryption, redaction and metrics read back.
- The TraceStore contract gained the S3 backend and five S3-specific tests: write versus read credentials, encryption, throttling and partial failure, writing outcomes with write-only credentials, and config refusals. New unit tests cover the command monitor and the Prometheus output.

## Design changes

- **`OutcomeRecord`** carries `agent`, `ticket_key` and `run_started`, so write-only credentials can place an outcome next to its run.
- **`ObjectTraceStore`** takes an optional separate `read_backend`.
- **New config:** `[intake.monitoring]`, `[feedback.grading]`, `[feedback.self_improvement]`, `models.stages.improve`, and the `monitor.command` and `trace_store.s3` aliases.
- **Defaults I chose while you were offline** (all are config values):
  - draft-only monitoring intake;
  - calibration threshold 0.8 with a tolerance of 1;
  - self-improvement limited to skills and agent definitions.

## Go-live notes for S3 (lifecycle is the bucket's job, not the factory's)

- Enable object versioning or Object Lock in governance mode to back up immutability.
- Set lifecycle expiry to your classification guidance's retention period. Transition to colder storage if the endpoint supports it.
- Write credentials need only `s3:PutObject` on `<bucket>/<prefix>*`. Read credentials need `s3:GetObject` and `s3:ListBucket` (prefix-scoped). Nobody gets `s3:DeleteObject` through the factory.
- A bucket policy should deny `PutObject` without `x-amz-server-side-encryption`.
