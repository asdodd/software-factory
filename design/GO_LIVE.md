# Going Live

Going live is a config-only change: edit `config/profiles/prod.toml` (and `staging.toml` for a dry run), set the secret environment variables, and run `factory config validate --profile prod` until it passes. `tests/unit/test_go_live.py` proves a filled-in prod profile builds every port from the real adapters with no code changes and no connection made.

Recommended order: `staging` with `dry_run = true` first (it logs every intended write), then `prod`.

## 1. Secrets (environment variables, never in files)

The variable names below are the ones the prod profile references. You can rename them there.

| Variable | Used by |
|---|---|
| `FACTORY_JIRA_TOKEN` | Jira personal access token for the factory's service account (`auth = "bearer"`). For basic auth, set `auth = "basic"` and `username`/`password` references instead. |
| `FACTORY_GITLAB_TOKEN` | GitLab token with `api` and `write_repository` on the agents' projects |
| `FACTORY_MODEL_API_KEY` | The government model endpoint or gateway |
| `FACTORY_TRACE_WRITE_KEY_ID`, `FACTORY_TRACE_WRITE_SECRET` | S3 credentials allowed `s3:PutObject` only (agents) |
| `FACTORY_TRACE_READ_KEY_ID`, `FACTORY_TRACE_READ_SECRET` | S3 credentials allowed `s3:GetObject` and `s3:ListBucket` only (metrics, grading, humans) |
| `FACTORY_MAIL_PASSWORD` | Only if `intake.email` is on |

Any of these can be a file instead: `{file = "/run/secrets/name"}`.

## 2. Keys that `factory config validate --profile prod` requires

This list is generated from the validator.

**Connections**
- `plugins.ticket_system.settings.base_url`: the Jira base URL, for example `https://jira.internal`
- `plugins.git_host.settings.base_url`: the GitLab base URL. Optional: `api_url` (default `<base_url>/api/v4`), `clone_base_url` (default `base_url`), `watched_projects` (for `/followup` intake), `identity` (the bot's username).
- `models.providers.primary.settings.base_url`: an OpenAI-compatible endpoint ending in `/v1`, or your gateway's equivalent
- `trace_store.s3.endpoint_url` and `trace_store.s3.bucket`; set `trace_store.s3.encryption.kms_key_id` for SSE-KMS (or `mode = "sse-s3"`)

**Identity**
- `collaboration.factory_identity`: the account the factory posts as. Its comments are never read as replies.

**Jira workflow mapping** (the factory's logical state on the left, your Jira's name on the right)
- `workflow.statuses.ready`, `in_progress`, `spec_review`, `in_review`, `needs_info`, `blocked`, `failed`, `done`. Also `stalled`, which is optional and defaults to unset.
- `workflow.fields.lease_owner`, `lease_expires`, `repo`, `acceptance_criteria`, `verify_steps`, `sponsor_team`, `source_ref`, `fingerprint`: the `customfield_NNNNN` ids. Lease, source_ref and fingerprint can be hidden text fields.
- `workflow.link_types.relates`, `blocks`, `duplicates`: your issue link type names
- `workflow.ticket_url_template`, for example `https://jira.internal/browse/{key}`
- Optional: `workflow.transitions.<state>` where the target status alone is ambiguous, and `workflow.priority_order`

**Labels**
- `labels.ready`, `labels.failed`, `labels.needs_info`. Optional: `stalled`, `spec_approved`. Each agent may override these in `config/agents/<name>.toml`.

**Models** (any provider name under `models.providers`; use a different one for review)
- `models.stages.triage.model`, `spec`, `implement`, `review`, `respond`, `intake`, `grading`, `improve`. Only the stages whose features are on are validated.
- `models.stages.implement.harness`: `claude` (the default), `codex`, `cline`, `jam-claude` or `jam-codex`.

**Requests and capabilities** (these have defaults, but set them for your endpoint)
- `models.budgets.requests_per_hour` (your endpoint's limit) and `max_wait_seconds`; `limits.max_requests_per_ticket`
- `[capabilities] subagents` and `internet`: these decide how the rules and skills read, and whether Claude Code's Task tool is enabled

## 3. Also review (they have safe defaults)

- **`[collaboration.allowed_commenters]`:** these ship empty, so the agents ignore every reply until you add users, groups or roles.
- **`[notifications]`:** who is mentioned for spec review, MR ready, failed and blocked tickets. Set `plugins.notifier.settings.mention_format` (Jira Server/Data Center uses `[~{user}]`).
- **`config/agents/*.toml`:** real filters, repos, service accounts and teams. The shipped agents are examples with `SIM` project values.
- **`[intake.email]`** (if used): `address`, `allowlisted_senders`, `user_map`, and `require_authenticated_sender = true` if your mail server adds `Authentication-Results`. Then set `plugins.mailbox.settings` `imap_host`, `smtp_host` and `processed_folder`.
- **`[intake.gitlab]`:** `allowed_authors`, and `plugins.git_host.settings.watched_projects`.
- **`[feedback.self_improvement].repo`:** the factory repo's GitLab path, if self-improvement is on.
- **`[git.host_credentials]` and `git.url_rewrites`:** for submodules on other hosts.
- **`[limits]`, `[gates]` and polling intervals:** tune after the first week of metrics (`factory metrics`).

## 4. Infrastructure checklist

- **`git` 2.20 or later** on the worker box, used for submodule `--depth` and `--jobs`.
- **The sandbox root** (`plugins.sandbox.settings.root`) on a disk with room for clones.
- **CLI agents, if used** (`claude`, `codex`, `cline`, `jam`): installed on the worker's `PATH`. Check the Cline flags against the installed version, and override `settings.args` if they differ.
- **S3 bucket** (the bucket enforces retention; the factory never deletes):
  - turn on versioning or Object Lock in governance mode;
  - set lifecycle expiry to your classification retention period;
  - add a bucket policy that denies `PutObject` without server-side encryption;
  - grant no `DeleteObject` to either credential.
- **Protected branches and code owners** in GitLab for lint config, test config and CI files. CI must use the target branch's lint and test configuration.
- **Scheduling.** Run `factory run --all-agents --loop` and `factory intake --loop` as services. Run `factory grade` and `factory improve` on a schedule, after `factory grade --calibrate labels.jsonl` reports a trusted grader.
- **Never contacted during this build:** Jira, GitLab, mail servers, model endpoints, S3. Each real adapter passed its contract suite against fixtures only.
