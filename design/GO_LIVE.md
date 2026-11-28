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
- `workflow.statuses.ready` (for example `Open`) and `workflow.statuses.in_progress` (for example `In Progress`). Humans own every other status change (for example to `In Review` and `Complete`). The factory claims from `ready` and only ever moves a ticket to `in_progress`, when implementation starts. Every other state is a label.
- `workflow.fields.lease_owner`, `lease_expires`, `repo`, `acceptance_criteria`, `verify_steps`, `sponsor_team`, `source_ref`, `fingerprint`: the `customfield_NNNNN` ids. Lease, source_ref and fingerprint can be hidden text fields.
- `workflow.link_types.relates`, `blocks`, `duplicates`: your issue link type names
- `workflow.ticket_url_template`, for example `https://jira.internal/browse/{key}`
- Optional: `workflow.transitions.<state>` where the target status alone is ambiguous, and `workflow.priority_order`

**Labels**
- Defaults are in `config/base.toml`. Change any of them per profile or per agent (`[overrides.labels]`).
- `ready` (a human applies it), `claimed` (the lock, `ai-claimed-{agent}`), `needs_info` (bounced or waiting on answers), `spec_review` and `spec_approved`, `in_review`, `blocked`, `failed`, `stalled`. Any value may contain `{agent}`.
- A ticket with any state label is not claimed as new work. To retry, a human removes the label, and sets the status back to `ready` if the agent had moved it to `in_progress`.

**Models** (any provider name under `models.providers`; use a different one for review)
- `models.stages.triage.model`, `spec`, `implement`, `review`, `respond`, `intake`, `grading`, `improve`. Only the stages whose features are on are validated.

## 3. Also review (they have safe defaults)

- **`[collaboration.allowed_commenters]`:** these ship empty, so the agents ignore every reply until you add users, groups or roles.
- **`[notifications]`:** who is mentioned for spec review, MR ready, failed and blocked tickets. Set `plugins.notifier.settings.mention_format` (Jira Server/Data Center uses `[~{user}]`).
- **`config/agents/*.toml`:** real filters, repos, service accounts and teams. The shipped agents are examples with `SIM` project values.
- **`[intake.email]`** (if used): `address` and `allowlisted_senders`. The `From:` header is trusted. The reporter is the Jira user found by the sender's address, so the service account needs permission to browse users. Then set `plugins.mailbox.settings` `imap_host`, `smtp_host` and `processed_folder`.
- **`[intake.gitlab]`:** `allowed_authors`, and `plugins.git_host.settings.watched_projects`.
- **`[feedback.self_improvement].repo`:** the factory repo's GitLab path, if self-improvement is on.
- **`[git.host_credentials]` and `git.url_rewrites`:** for submodules on other hosts.
- **`[repos."group/name"]`** (optional, in the factory's config, never in the target repo): `standards`, the `commands` the factory re-runs as gates (setup, lint, test, verify), `evidence`, `protected_paths`, `coverage_report`, `mutation`. With no entry, the agent runs the repo's own tooling and the repo's CI judges the merge request.
- **`[limits]`, `[gates]` and polling intervals:** tune after the first week of metrics (`factory metrics`).

## 4. Infrastructure checklist

- **`git` 2.20 or later** on the worker box, used for submodule `--depth` and `--jobs`.
- **The sandbox root** (`plugins.sandbox.settings.root`) on a disk with room for clones.
- **CLI agents** (`claude` by default; `codex`, `cline`, `jam` if used): installed on the worker's `PATH`, with `python3` available for the agent kit's hook. Check the Cline flags against the installed version, and override `settings.args` if they differ.
- **S3 bucket** (the bucket enforces retention; the factory never deletes):
  - turn on versioning or Object Lock in governance mode;
  - set lifecycle expiry to your classification retention period;
  - add a bucket policy that denies `PutObject` without server-side encryption;
  - grant no `DeleteObject` to either credential.
- **Protected branches and code owners** in GitLab for lint config, test config and CI files. CI must use the target branch's lint and test configuration.
- **Scheduling.** Run `factory run --all-agents --loop` and `factory intake --loop` as services. Run `factory grade` and `factory improve` on a schedule, after `factory grade --calibrate labels.jsonl` reports a trusted grader.
- **Never contacted during this build:** Jira, GitLab, mail servers, model endpoints, S3. Each real adapter passed its contract suite against fixtures only.
