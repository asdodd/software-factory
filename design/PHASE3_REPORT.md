# Phase 3 Report: Email and GitLab-Comment Intake

Branch: `claude/software-factory-phase3`, created from the Phase 2 head of `claude/software-factories-impl-j3d5so`.

Status: **Phase 3 is complete and ready for your review.** I'll wait for your go-ahead before starting Phase 4. Everything runs against the fake mailbox, the fake git host and scripted models.

## How to check it

```sh
python3 -m pytest -q                       # 163 tests + 41 subtests, network guard on
factory scenario run tests/scenarios/cases/email_allowlisted_end_to_end.toml
factory intake --enable intake.email --enable intake.gitlab_comments   # one polling cycle (sim fakes start empty)
```

## What works

**Email intake** (`factory/core/intake_sources.py`, enabled by the `intake.email` feature):
- **What it reads.** Only mail addressed to the bot (`intake.email.address` in To or Cc) is processed.
- **Loop protection.** It skips:
  - the bot's own mail;
  - ignored senders (mailer-daemon, postmaster, noreply and similar, configurable);
  - mail marked `Auto-Submitted` (anything but `no`);
  - mail with `Precedence: bulk/list/junk/auto_reply` or `X-Autoreply` / `X-Autorespond` headers;
  - duplicate deliveries of the same message id.

  Skipped mail is counted by reason (metric `email_ignored`) and logged by sender address only.
- **Drafting.** The full thread and any allowed attachments go to the `intake` model stage, which drafts the title, description, repo guess, components, acceptance criteria and verify steps.
  - Attachments are checked by the access guard and a size limit before the model sees them. Withheld ones are only counted.
  - A repo guess outside the known repos is dropped.
  - The drafting prompt tells the model the thread is untrusted. Only the draft fields are read from its output; anything else it returns (labels, assignee, priority, policy) is ignored.
  - If drafting fails, the subject and body are used.
- **Trust and labels.** The shared readiness checklist decides the verdict. Only the sender allowlist decides trust:
  - wildcards are allowed;
  - optional `require_authenticated_sender` also requires `dkim=pass` or `dmarc=pass` in `Authentication-Results`;
  - the email body has no say.

  With `intake.email.auto_label` on, a ready ticket from a trusted sender is labeled and moved to ready. With it off (the default, draft-only), the factory proposes the verdict in a comment and a human applies the label.
- **Ticket fields.** The requester is the reporter, mapped through `user_map` to a Jira user, or by address if unmapped. The bot is the creator. The thread id is stored in `source_ref`.
- **Reply.** The confirmation goes to the original sender, plus people already on the thread (excluding the bot and ignored senders). For a mailing list (`List-Id`) or more than `max_reply_recipients` people, it goes to the sender only. The reply contains:
  - the ticket link (`workflow.ticket_url_template`);
  - "marked ready", "a person will confirm", or the numbered missing items;
  - an instruction to add further information as a Jira comment, and a note that email replies aren't read.
- **Replies on a thread that already has a ticket** change nothing (counted and logged) unless `intake.email.allow_updates` is true. In that case the reply is added to the ticket as clearly marked untrusted content, and validation warns.

**GitLab-comment intake** (enabled by the `intake.gitlab_comments` feature):
- A comment whose first word is the keyword (`/followup`) creates a follow-up ticket.
  - The summary is the rest of the first line.
  - List items on later lines become acceptance criteria.
  - The repo is the comment's project, and the description links back to the thread.
- This needs no model: it's deterministic, as the spec asks.
- Allowed authors (`intake.gitlab.allowed_authors`) can be labeled automatically when `intake.gitlab.auto_label` is on. It's off by default (draft-only).
- Comments by the factory itself, any agent account and any service or bot principal are ignored.
- The factory replies in the thread with the new ticket, or with the existing one if it's a duplicate.
- Polling uses a stored cursor, and each event id is processed once.

**Dedupe and idempotency** (shared): one intake service keys on source plus message, thread or event id, so retries never duplicate. It fingerprints (repo, location, normalized summary) and comments on the matching ticket instead of creating a duplicate.

**Override tracking:** every intake verdict is recorded. `reconcile()` runs on each intake cycle and records:
- a human applying the label against a not-ready verdict;
- a human removing a label the factory applied;
- a human confirming a proposed-ready ticket.

These go to the state store and the metrics (`intake_overrides`, `intake_confirmations`), tagged by source, for the Phase 4 override-rate metric. Tracking ends after `intake.override_window_days`.

**CLI:** `factory intake [--loop]` polls the enabled sources and reconciles overrides.

## Bug found and fixed

A flaky failure in the Phase 2 `mr_max_fix_rounds` scenario was a real bug. Git's stat cache doesn't notice an edit that keeps the file the same size within the same second as the checkout, so a harness's edit could be silently dropped. The factory now stages by content wherever it decides what changed (the isolated copy and the agent's commit), and no longer preserves mtimes when copying edits back. `tests/unit/test_workspace.py` is the regression test. Error messages from git now include stdout when stderr is empty.

## Test results

- 163 tests and 41 subtests pass, three runs in a row; ruff reports no issues.
- 86 scenarios, 16 of them new in Phase 3:
  - **Email:** an allowlisted email end to end (draft, label, route, claim, MR); draft-only with a recorded confirmation; prompt injection in the body; duplicate delivery and cross-thread dedupe; thread reply ignored; thread reply allowed with a warning; loop protection (5 kinds); reply hygiene (small thread, large thread, mailing list); restricted email attachments kept from the model; override against a not-ready verdict; override by removing a factory label; authenticated-sender requirement.
  - **GitLab:** `/followup` follow-up; auto-label for allowed versus other authors; idempotency and dedupe; ignoring the factory's own comments and comments without the keyword.
- The Phase 1 scenario "disabled feature never contacted" still proves the mailbox isn't instantiated when email intake is off.

## Design changes

- **GitHost port:** `comment_on(event, body, idempotency_key)`, to reply where a comment event happened.
- **Write library:** `mail_reply` and `host_comment`, so intake writes are dry-run aware and redacted like every other write.
- **Config:**
  - `intake.email`: `address`, `require_authenticated_sender`, `user_map`, `ignore_senders`, `max_reply_recipients`, `max_attachment_bytes`;
  - `intake.gitlab`: `auto_label`, `allowed_authors`, `reply`;
  - `intake.override_window_days`;
  - `workflow.ticket_url_template`.
- **`IntakeEvent.auto_label`:** separates "the sender is trusted" from "this source may label automatically".
- **The `intake` model stage** is now only required when email intake is on, since GitLab intake doesn't use a model.

## Deferred

- **Phase 4:** monitoring intake (the same intake service, with dedupe already in place), metrics export including the intake override rate, the S3 trace backend with a fake S3, grading and self-improvement.
- **Phase 5:** the real IMAP/SMTP mailbox and GitLab adapters, written to these contracts and tested only against fixtures.

## Needs your decision

1. **Sender identity.** Is `Authentication-Results` (dkim or dmarc pass) available from your internal mail server? If so, I recommend turning on `require_authenticated_sender`, because otherwise the allowlist trusts the `From:` header as written.
2. **Mapping emails to Jira users.** `user_map` maps addresses to Jira users by hand. Would you rather look users up by email in Jira? That's possible in the real adapter, but it's an extra API call per email.
3. **GitLab follow-up project.** Follow-up tickets go to `intake.gitlab.default_project`. Should they instead go to a project mapped from the GitLab group?
4. **Open from earlier phases:** the bounce status, approving specs by label, the overlapping agent filters, the location of the repo rules file, the stall status, replies when auto-fix is off, and whether missing coverage or mutation setup should fail the gate.
