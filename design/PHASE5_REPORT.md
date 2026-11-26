# Phase 5 Report: Real Adapters (Fixtures Only)

Branch: `claude/software-factory-phase3`, continued while you were offline.

Status: complete, and stopped there as the spec says. No real system was contacted. The go-live keys are in `design/GO_LIVE.md`.

## Adapters written to the contract suites

| Port | Real adapter | Tested against |
|---|---|---|
| TicketSystem | `factory/adapters/jira/ticket_system.py`: Jira Server/DC REST v2. Neutral filter to JQL (labels any/all/none, project, component, issue type, priority, status, custom fields) plus a validated native JQL escape hatch; custom fields mapped from `workflow.fields`; transitions found by target status; comment idempotency by comment property; commenter groups looked up and cached; attachments downloaded. | The shared TicketSystem contract (the same tests the fake passes) on a hand-written Jira emulator that applies the JQL the adapter emits |
| GitHost | `factory/adapters/gitlab/git_host.py`: GitLab REST v4. Merge requests (idempotent by source branch), discussions, replies, suggested changes on diff positions, issue and MR notes, project comment events with a per-project cursor; clone, pull and push through the shared git CLI wrapper with a token credential helper. Note idempotency uses an invisible marker. | The shared GitHost contract on a GitLab emulator plus local repositories |
| Mailbox | `factory/adapters/mail/imap_smtp.py`: imaplib/smtplib. UNSEEN polling, thread roots from References/In-Reply-To, attachments, move to a processed folder; replies set `Auto-Submitted: auto-replied` and a Message-ID derived from the idempotency key. | The shared Mailbox contract on in-process fake IMAP and SMTP servers |
| ModelProvider | `factory/adapters/models/openai_compatible.py`: chat completions with tools and usage, retries on 429 and 5xx. `factory/adapters/models/replay.py`: record-and-replay keyed by request. | The shared ModelProvider contract (scripted, OpenAI on an emulator, replay) |
| Accounting | `factory/adapters/models/sqlite_accounting.py` | The shared Accounting contract (memory and SQLite) |
| TraceStore | `factory/adapters/trace/s3.py` (Phase 4) | The TraceStore contract on the fake S3 |

All real HTTP adapters share `factory/adapters/http.py`. It provides JSON, timeouts, a CA bundle, bounded retries that honor `Retry-After`, errors mapped to port errors without secrets, and an injectable transport, so tests never open a socket. The network guard stays on for every test.

## Proof that going live is config-only

`tests/unit/test_go_live.py` copies `config/`, fills every empty value in `prod.toml` with placeholders, validates, and builds every port through the container. The result is Jira, GitLab, OpenAI-compatible, S3, SQLite state and accounting, and the ticket-comment notifier, all built from config alone with no connection. The unfilled profile fails validation and lists exactly what's missing.

## Test results

- 222 tests and 46 subtests pass; ruff reports no issues.
- New: 38 contract tests across Jira, GitLab, mail, models and accounting, plus the go-live tests.

## Known limits (worth checking on the box)

- **Jira project roles** aren't resolved from the API, which would mean one call per project. Users and groups are. Commenter rules using `project_roles` only match if the real adapter is extended.
- **Jira comment properties on create** are documented for Server/DC 7.x and later. If your version ignores them, comment idempotency falls back to retries possibly double-posting. Tell me the Jira version and I'll switch to a body marker.
- **GitLab comment events** are read per watched project from the events API. Very busy projects may need a smaller poll interval.
- **The OpenAI-compatible adapter** doesn't stream, since the factory doesn't need streaming from the upstream endpoint; the local gateway streams to CLI agents itself.
- **Real binaries and endpoints weren't run:** `claude`, `codex`, `cline` and `jam` were exercised with fakes only (Phase 1 addendum).

## Defaults I chose while you were offline

All of these are config values:
- monitoring intake is draft-only;
- the grader needs 0.8 agreement (tolerance 1);
- self-improvement may edit only skills and agent definitions;
- `require_authenticated_sender` stays off but is recommended in `GO_LIVE.md`;
- GitLab follow-ups go to one default project.
