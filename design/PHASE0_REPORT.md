# Phase 0 Report: Design

Status: **waiting for your review.** Per spec §13 and §14, Phase 1 does not start until you've reviewed this design and answered the open questions below. Each question comes with my recommendation, so you can answer "agree" or change only what you disagree with.

## Deliverables

| Spec item (§13 Phase 0) | Where |
|---|---|
| Full flow diagram | `design/flow.puml` (pipeline, with swimlanes for deterministic, model and human steps) and `design/collaboration.puml` (Jira reply loop and MR comment loop) |
| Port interfaces | `factory/ports/*.py` (real `typing.Protocol` classes and value types) and `design/ports.puml` |
| Component and dependency view | `design/components.puml` |
| Config schema with annotated example | `factory/config/schema.py` (typed, defaults from §8.3), `factory/config/validate.py` (strict: unknown keys and wrong types are errors that name the key), `config/base.toml` (annotated), `config/profiles/{sim,staging,prod}.toml`, `config/agents/{frontend-bot,docs-bot}.toml` |
| Feature-flag table | `factory/features/definitions.py` (single source of truth) and the table below |
| Jira status and workflow mapping format | `[workflow]` section (below), `design/workflow-states.puml` |

To verify: `python3 -m unittest discover -s tests -t .` runs 19 design checks, all passing. They check that every example config validates against the schema; that unknown keys and bad types are rejected with dotted paths; that secrets in config are references only; that the defaults match §8.3; that non-sim profiles ship empty commenter lists; that agents cannot override `access_policy`; that feature dependencies are known and acyclic; that every feature's config key exists; that core, ports, config and features never import `factory.adapters`; that every port is a Protocol; that every stage in every profile and agent names a declared provider and harness; and that a harness task carries no upstream credentials.

No third-party dependencies are used so far, at runtime or in tests.

## Key design decisions

1. **Core uses logical states; config supplies native names.** Core moves tickets between the fixed `LogicalStatus` values (`ready`, `in_progress`, `spec_review`, `in_review`, `needs_info`, `blocked`, `stalled`, `failed`, `done`). `[workflow.statuses]` maps each one to a native Jira status name. The adapter finds the Jira transition that leads to that status. `[workflow.transitions]` is an optional override for workflows where that's ambiguous. Custom fields work the same way: core uses logical names (`lease_owner`, `lease_expires`, `repo`, `acceptance_criteria`, `verify_steps`, `sponsor_team`, `source_ref`, `fingerprint`), and `[workflow.fields]` maps them to `customfield_NNNNN`. Link types work the same way too.
2. **Leases are stored on the ticket.** `lease_owner` and `lease_expires` live in two custom fields. Claim works like this: transition, set the assignee and lease, re-read, and confirm. This means the ticket system stays the source of truth and workers stay stateless.
3. **Idempotent comments.** Every write carries an idempotency key. Real adapters embed it as a hidden marker in the comment or MR body and check for it before posting, so retries are safe without local state.
4. **Proposed extra port: `StateStore`.** It holds intake cursors, the idempotency keys of processed messages, and paused-run state. The spec's port list has nowhere for these. In production it would use `sqlite3` from the standard library.
5. **The trace store is split into `TraceWriter` and `TraceReader`.** Agents only receive a writer, built with write-only credentials, so "agents can never read back or delete" is enforced by the types as well as by IAM. `trace_store.backend` selects the backend, not `plugins.*`, as §6.10 specifies.
6. **Model budgets are enforced in one place.** A core wrapper around `ModelProvider` checks `Accounting` before each call, then records usage. Adapters stay thin.
7. **Agent overrides are a closed set.** An agent file may override only `labels`, `output`, `jira_comments`, `mr`, `limits`, `models` and `features`. It may *add* deny patterns (`extra_deny_patterns`) but has no way to name `access_policy`. Any attempt is an unknown-key error.
8. **Branch-only agents must turn off `mr.*`.** `docs-bot` shows this. The `mr.*` features default to on and require `merge_request` mode, so a `branch_only` agent has to switch them off explicitly. Otherwise validation fails with a message naming the conflict, as §10 requires.

## Bring your own harness and bring your own model

A harness is only the inner loop of a model stage. Everything that makes the process shared stays in the factory and is identical whichever harness or model is used: readiness, claim, instructions and base skills, the access policy, the PR gates, push, comments, and traces.

**Bring your own model.** You can declare any number of named providers under `[models.providers.<name>]`, each a `ModelProvider` plugin: the OpenAI-compatible adapter for the government endpoint or a gateway, replay, scripted, or your own `pkg.module:Class`. Each stage picks a provider and a model (`[models.stages.implement] provider = "...", model = "..."`). Agents can override this per stage, so the reviewer can use a different provider from the implementer.

**Bring your own harness.** A new `AgentHarness` port (`factory/ports/harness.py`) has one method, `run(task) -> result`. Each `[harnesses.<name>]` entry selects an adapter:
- `harness.builtin` is the in-house tool loop, where every tool call goes through the `AccessGuard`.
- `harness.command` wraps any CLI agent using an argv template with placeholders (`{workspace}`, `{instructions_file}`, `{gateway_url}`, `{gateway_token}`, `{model}`, ...). `config/base.toml` has commented examples for Aider and Claude Code.
- `harness.scripted` is for scenario tests.
- Your own adapter can be given as a dotted path.

`[models.stages.<stage>] harness = "<name>"` picks the harness per stage and per agent.

**How the factory stays in control of a harness it didn't write:**
1. **Isolated workspace.** The harness never sees the real clone. The factory builds a copy with the access policy already applied, and replaces git history with a single baseline commit, so `git log -p` or `git show` can't recover restricted files. Submodules are flattened into the copy as plain files.
2. **Loopback model gateway.** The harness gets a `127.0.0.1` URL and a per-run token, never the upstream credentials. The gateway speaks `openai_chat` and `anthropic_messages` and forwards to the configured `ModelProvider`. That is where budgets, per-ticket caps, attribution, redaction and trace capture apply. It works for any harness that lets you set a base URL, which most do.
3. **The factory carries the result back.** The factory, not the harness, diffs the copy against the baseline. It rejects changes to `.git`, submodule paths, restricted paths and protected paths, applies the rest to the agent branch in the real clone, re-runs lint and tests there, and runs the PR gates.
4. **A small result contract.** The harness may write `result.json` (`done | needs_info | blocked | failed`, a summary, and questions), which drives the Jira reply loop. Otherwise the exit code decides the outcome. The harness never gets ticket-system or git-host credentials, so it can't comment, push or merge.

**Limits to be honest about:**
- With the `local_dir` sandbox, an external harness process isn't network-isolated or confined to its directory. The environment scrub and the lack of credentials reduce what it can reach, but don't prevent it. The `bubblewrap` or `podman` sandbox plugin closes this gap: no network except the gateway socket, and a filesystem view limited to the workspace.
- `access_policy.mode = "deny_read"` keeps restricted files in place, which an external harness can't be trusted to respect. It will therefore be a startup error to combine `deny_read` with a non-builtin harness unless the sandbox enforces hidden paths. `strip` (the default) works with every harness.
- Per-tool-call auditing of an external harness's denied reads is only possible if the harness offers a hook that can call `factory guard check`. Where it does, the command adapter can wire it up. Otherwise, steps 1 and 3 are the enforcement.

## Jira status and workflow mapping format

```toml
[workflow]
claim_from = ["ready"]                 # logical states a worker may claim from

[workflow.statuses]                    # logical (fixed) -> your Jira status name
ready = "Ready for AI"
in_progress = "In Progress"
spec_review = "Spec Review"
in_review = "In Review"
needs_info = "Waiting for Info"
blocked = "Blocked"
stalled = "Open"
failed = "Open"
done = "Done"

[workflow.transitions]                 # optional; only where the target status is ambiguous
# in_review = "Submit for Review"

[workflow.fields]                      # logical field -> custom field id
lease_owner = "customfield_10001"
lease_expires = "customfield_10002"
repo = "customfield_10003"
# acceptance_criteria, verify_steps, sponsor_team, source_ref, fingerprint

[workflow.link_types]
relates = "Relates"
blocks = "Blocks"
duplicates = "Duplicate"

[labels]                               # global defaults; each agent can override
ready = "ai-ready"
failed = "ai-failed"
needs_info = "ai-needs-info"
stalled = "ai-stalled"
spec_approved = "ai-spec-approved"
```

Agent ticket filters use the neutral form (`labels_any/all/none`, `projects`, `components`, `issue_types`, `priorities`, `statuses`, `custom_fields` by logical name). There is an adapter-specific escape hatch (`native_adapter = "jira"`, `native_query = "..."`) that every other adapter rejects.

## Feature-flag table

A `features.X` key means key `"X"` in the `[features]` table. Precedence from lowest to highest: built-in default, global config, profile, agent definition, environment, CLI. `access_policy.enabled` is deliberately not a feature (§6.12).

| Feature | Default | Config key | Requires | Per-agent |
|---|---|---|---|---|
| `intake.jira` | on | `features.intake.jira` | - | no |
| `intake.email` | off | `features.intake.email` | - | no |
| `intake.email.auto_label` | off | `intake.email.auto_label` | `intake.email` | no |
| `intake.email.allow_updates` | off | `intake.email.allow_updates` | `intake.email` | no |
| `intake.gitlab_comments` | off | `features.intake.gitlab_comments` | - | no |
| `intake.monitoring` | off | `features.intake.monitoring` | - | no |
| `intake.routing` | off | `intake.routing.enabled` | - | no |
| `readiness_gate` | on | `features.readiness_gate` | - | yes |
| `triage` | on | `features.triage` | - | yes |
| `spec` | on | `features.spec` | `triage` | yes |
| `implement` | on | `features.implement` | - | yes |
| `review` | on | `features.review` | `implement` | yes |
| `verify` | on | `features.verify` | `implement` | yes |
| `monitoring` | off | `features.monitoring` | `intake.monitoring` | no |
| `feedback.metrics` | on | `features.feedback.metrics` | - | yes |
| `feedback.trace_store` | on | `features.feedback.trace_store` | - | yes |
| `feedback.grading` | off | `features.feedback.grading` | `feedback.trace_store` | no |
| `feedback.self_improvement` | off | `features.feedback.self_improvement` | `feedback.grading` | no |
| `jira_comments.post` | on | `jira_comments.post` | - | yes |
| `jira_comments.read_replies` | on | `jira_comments.read_replies` | `jira_comments.post` | yes |
| `mr.read_comments` | on | `mr.read_comments` | `output.mode=merge_request` | yes |
| `mr.respond` | on | `mr.respond` | `mr.read_comments`, `output.mode=merge_request` | yes |
| `mr.auto_fix` | on | `mr.auto_fix` | `mr.read_comments`, `output.mode=merge_request` | yes |
| `git.submodules` | on | `git.submodules.enabled` | - | yes |
| `dry_run` | off | `features.dry_run` | - | yes |

`intake.routing` is listed here because §6.8 says routing "can be toggled, and default[s] to off". The spec does not say whether `monitoring` depends on `intake.monitoring` or the reverse. I chose "monitors need intake to accept their findings".

## Decisions recorded

- **Deny markings:** `proprietary` only (path and content). No other default patterns.
- **Dependencies:** third-party packages are allowed, including `boto3` for S3 and `pytest` for tests. Every package is still pinned and listed.
- **S3:** keep dummy defaults for now. The S3 backend is tested only against the in-process fake.
- **Languages:** no language is modelled per repo. Each repo's rules file declares its own lint, test, coverage and verify commands, which may span several languages. Gates read standard formats (JUnit XML, LCOV or Cobertura). Fixtures include a mixed-language repo.
- **Harness and model:** bring your own harness and bring your own model, through the `AgentHarness` port, named model providers, the isolated workspace and the loopback gateway (see the section above).
- **Sandbox:** `local_dir`, rooted at a directory set in config, with a fresh subdirectory per run, a scrubbed environment, `resource` limits, process-group kill on timeout, and teardown. A `bubblewrap` or `podman` plugin can be added later as a config-only change.

## Open questions (§14), with recommendations

1. **Config format.** *Recommendation: TOML.* Python 3.11+ ships `tomllib` in the standard library, so the config needs no dependency on the offline box. YAML would need PyYAML to be approved and mirrored. The cost is that spec §6.10 and §8.3 examples are written in YAML; I translated them one to one. If you prefer YAML and approve PyYAML, only the loader and the example files change.
2. **Third-party dependencies.** *Recommendation: none at runtime.* Use `urllib.request` for HTTP (Jira, GitLab, model endpoint), `imaplib` and `smtplib` for mail, `sqlite3` for state and accounting, and `unittest` for tests. The one place a library pays for itself is **S3 SigV4 signing**. Either I hand-write a SigV4 signer (about 150 lines, contract-tested against AWS's published test vectors), or you approve `boto3`/`botocore`, which are large but standard and support a custom endpoint. My lean is to hand-write it to avoid a large dependency tree. `pytest` is optional; tell me if your mirror already carries it. Which do you prefer, and what is your approval and mirroring process?
3. **Plugin discovery.** *Recommendation: dotted class paths in config (`"package.module:Class"`), with short aliases defined in `config/base.toml`.* This works directly from a source checkout with no `pip install` and no packaging metadata. It's greppable, and `config validate` can check that the path resolves without instantiating anything. Adding an adapter means adding a module plus one config line. Entry points need every adapter to be installed as a distribution, which complicates offline development.
4. **Agent harness.** *Decided: bring your own harness (see the section above).* The built-in harness remains the default: a small in-house tool loop (system prompt + base skills + rules, then model ↔ tools: read file, search, write file, run command, ask question), speaking OpenAI-compatible chat with tool calling. Every tool call passes through `AccessGuard` and the budget wrapper. Off-the-shelf harnesses, such as Claude Code, Aider or OpenHands, would bypass the guard, assume a specific vendor or network access, and are hard to make deterministic for scenario tests. External harnesses plug in through `harness.command`.
5. **Sandbox technology.** What does the box offer: rootless Podman/Docker, bubblewrap, VMs, or only plain directories? *Recommendation:* use local directories for Phase 1 (as the spec says), then use rootless containers with no network if they're available.
6. **First target repos and languages.** I need these so the fixture repos (and lint, test and coverage commands) resemble the real ones.
7. **Trace storage and retention rules** under your classification guidance, including whether `capture_level = "full"` (prompts and responses) is permitted.
8. **Is `git` available on the box, and which version?** The real GitLab adapter runs `git` with fixed argument lists. Submodule `--depth` and `--jobs` need git 2.20 or later.
9. **Jira and GitLab versions** (Server/Data Center vs Cloud, and the API version), so the fixtures match. Jira Server uses REST v2 with bearer PATs. Is that right for you?
10. **Which filter dimensions your Jira actually uses.** The neutral filter already covers labels, projects, components, issue types, priority, status and custom fields. Anything else, such as fix versions, sprints or epics?
11. **Are the §8.3 defaults right for you?** That covers lease 120 min, 2 attempts, 2M tokens per ticket, 15 files and 600 diff lines, 5-minute polling, and a 72-hour stall timeout. I added `max_denied_access_attempts = 5` and `git.clone.timeout_seconds = 600`.
12. **Proprietary rule.** I defaulted to matching both path and content. Should other markings join the default deny list? Examples: classification banners, `export-controlled`, `ITAR`, `NOFORN`, or company-confidential headers. I can't guess your marking conventions.
13. **S3 specifics.** Is your endpoint S3-compatible with path-style addressing? Which encryption is available (SSE-KMS or SSE-S3)? Can separate write-only and read-only credentials be issued?
14. **Submodule hosts.** Do any submodules live on other hosts that need separate credentials or URL rewrites (for example SSH URLs that should be rewritten to HTTPS)?

## Phase 1 plan once you approve

1. Write the config loader (layering, env and CLI overrides, secret resolution and redaction, `config validate`, `config show`), the feature resolver (dependency errors, `factory features [--agent]`), the registry, and the CLI skeleton.
2. Build fake adapters for every port, a network guard (it fails on any non-local socket), and a declarative scenario harness.
3. Build the pipeline core: readiness checklist, claim and push library (with `dry_run`), state machine, dedupe, both output modes, and Jira status comments, all driven by the scripted model provider.
4. Add harness plumbing: workspace isolation, the loopback gateway (both dialects), the diff-back step, and the scripted and command harnesses. The command harness is tested with a tiny fake CLI agent script. The builtin tool loop follows in Phase 2 with the model stages.
5. Add safety and code access: `PolicyAccessGuard` and the no-proprietary-files base skill, the local trace store, and clone, pull and submodule handling using real local git fixtures.
6. Exit criteria (§13): the §12 happy-path and failure scenarios pass in `sim`, and features can be toggled from config and CLI. `frontend-bot` and `docs-bot` run side by side.
