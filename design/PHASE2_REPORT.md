# Phase 2 Report: Collaboration Loops, Review, and Quality Gates

Status: **Phase 2 is complete and ready for your review.** I'll wait for your go-ahead before starting Phase 3. Everything here runs against fakes and scripted models; no real system is contacted.

## How to check it

```sh
python3 -m pytest -q                         # 145 tests + 41 subtests, network guard on
ruff check factory tests
factory scenario run tests/scenarios/cases/reply_resumes.toml tests/scenarios/cases/mr_change_request_fixed.toml
factory config validate --profile prod       # now also lists respond.model and collaboration.factory_identity
```

## What works

**Jira reply loop** (`factory/core/collaboration.py`, `poll_replies`):
- When a ticket is paused for information, the agent records the questions, the round number, and a comment cursor that starts *after* its own question. Each paused ticket is then polled at `jira_comments.poll_interval_seconds`.
- A reply from an allowed commenter re-claims the ticket (new lease), removes the needs-info label, posts a "Resumed" comment, and runs again with every answer so far. On resume the agent skips triage and keeps the approved spec. The answers reach the harness in an "Answers from the ticket" section of its instructions.
- Further questions count as another round. Going past `max_question_rounds` stalls the ticket. So does no answer within `stall_timeout_hours`: the agent applies the stalled label, transitions to stalled, releases the lease, and comments.
- Allowed commenters are users, groups, project roles, the reporter or the assignee, each optional, with per-channel overrides. The agent skips, without reading them, comments from:
  - the factory's own identity (new key `collaboration.factory_identity`, required when any read feature is on);
  - every agent's service account;
  - configured bots;
  - any service or bot principal.

  Anyone else's comments are ignored, logged by author id only, and counted (metric `comments_ignored`). An empty rule ignores everyone.
- Comment bodies are data. Nothing in the factory reads limits, filters, config, permissions or protected paths from them. A scenario sends a reply that says to raise `max_attempts`, disable review and edit `.gitlab-ci.yml`; the gates still reject the change.

**MR comment loop** (`poll_mrs`):
- Each MR the agent opened is watched. The agent polls new comments with a cursor at `mr.poll_interval_seconds`. It stops watching when the MR is merged or closed, or after `inactivity_timeout_days`, and says so on the ticket.
- Comments from allowed reviewers go to the new **`respond`** model stage, which classifies each one as a question, change request, ambiguous or acknowledgement:
  - A **question** gets a reply in the thread (`mr.respond`).
  - An **ambiguous** comment gets one clarifying question.
  - A **change request** (`mr.auto_fix`) re-runs the harness on the agent's own MR branch, with the request in its instructions. The fix goes through every gate, the agent pushes a fix commit to that branch only (`Writes.push_branch` refuses any branch outside the agent's prefix), and it replies with the commit and what changed.
  - Once `mr.max_fix_rounds` is reached, the agent replies that a human should take over. A fix that can't pass the gates gets a reply, not a failed ticket.
- **`/apply`** deterministically applies the reviewer's outstanding suggested changes on a new branch, `<branch>-apply-N`. It opens a follow-up MR *targeting the agent's own MR branch*.
- The agent never resolves threads (`resolve_threads` stays false), and when `jira_comments.post` is on it summarizes MR feedback on the ticket.

**Reviewer:**
- The review prompt tells the model it didn't write the change.
- Findings that carry a path, line range and replacement are posted as suggested-change blocks; the others go into the MR description.
- Validation warns when the review stage uses the same provider and model as implement.
- The MR's **Architecture diff** section now includes a deterministic PlantUML component diagram built from the changed files. It works across Python, JS/TS, Go, Java/Kotlin, C/C++ and Rust, using import statements. The reviewer's own text is appended after it.

**Quality gates** (`factory/core/quality.py`):
- **Coverage on changed lines** uses the repo's own coverage report, declared as `coverage_report` in the repo rules (LCOV or Cobertura XML, detected from the file). Only lines the diff *adds* and the report instruments are counted.
- **Mutation score on changed code** uses a `[mutation]` block in the repo rules: a command, a report path, and a format, either the Stryker mutation-testing-report schema or `{"score": x}`. Only mutants in changed files count.
- Both use thresholds from `[gates]`. A repo that declares neither still passes, and the MR says plainly that coverage and mutation weren't measured.

**Notifications:**
- Configured in `[notifications]`: recipients for spec review, MR ready, failed and blocked, plus the reporter.
- The prod and staging profiles use the new `notifier.ticket_comment`, which mentions people in a ticket comment. The mention syntax is config: `[~{user}]` for Jira Server/Data Center.
- A failed notification never fails a ticket.

**Lease renewal:** the lease is renewed at the start of each attempt and before publishing, so a long run isn't reclaimed under the worker.

## Test results

- 145 tests and 41 subtests pass; ruff reports no issues.
- 70 scenarios, 23 of them new in Phase 2:
  - **Jira replies:** an answer resumes the ticket; poll interval respected; unanswered question stalls; non-allowed commenter ignored; the agent's own, other agents' and bot comments ignored; group-based allow; a reply can't change config; empty rule ignores everyone; question-round limit; read_replies off.
  - **MR comments:** question answered; change request fixed on the agent's own branch, with `main` untouched; ambiguous comment gets a clarifying question; fix-round limit; non-allowed reviewer ignored; merged MR stops watching; inactivity timeout.
  - **Review, spec and gates:** suggestions plus `/apply` follow-up MR; spec approval then resume with the spec, with notifications to the right people; coverage gate fail and pass; mutation gate; same-model review warning.
- New unit tests cover the LCOV, Cobertura and Stryker parsers, the architecture diagram, the ticket-comment notifier and the state index.

## Design changes

- **New config keys:**
  - `collaboration.factory_identity`
  - `models.stages.respond`
  - the `[notifications]` section
  - repo rules: `coverage_report` and the `[mutation]` block
- **Interface changes:** `Principal.roles`, so project-role rules work, and `AdapterContext.port`, so an adapter can compose other ports (the ticket-comment notifier uses the ticket system).
- **The notifier port is now always on**, since failures and MR-ready alerts don't depend on the spec feature.
- **One implement-and-gate loop** (`_Run.implement`) now serves new tickets, resumed tickets, review fixes and `/apply`. Fixes and applies can't skip any gate.
- **New outcomes:** `fix_pushed`, `fix_failed` and `replied`.

## Deferred

- **Phase 3:** email and GitLab-comment intake, draft-only override tracking, and the email trust and prompt-injection scenarios.
- **Phase 4:** monitoring intake, metrics export, the S3 trace backend with a fake S3, grading and self-improvement.
- **Reviewing human-authored MRs:** the reviewer only runs on the agent's own changes. If you want the factory to review people's MRs too, `/apply` already opens follow-up MRs instead of pushing to someone else's branch.
- **Classification relies on a model.** Deciding whether an MR comment is a question or a change request is the one judgment call made by a model, which the spec allows since it isn't deterministic work. A wrong classification can only cost a reply or a gated fix attempt on the agent's own branch; it can't produce an unsafe write.

## Needs your decision

1. **Stall status.** A stalled ticket moves to `workflow.statuses.stalled`, which is `Open` in sim, and gets the stalled label. Is that the status you want?
2. **Change requests with auto-fix off.** When `mr.auto_fix` is off but `mr.respond` is on, the agent replies that a human will make the change. Should it stay silent instead?
3. **Missing coverage and mutation reports.** These are currently notes in the MR, not failures. Should a missing coverage or mutation setup fail the gate for some repos?
4. **Questions from Phase 1 still open:** the bounce status, approving specs by label, the overlapping agent filters, and the location of the repo rules file.
