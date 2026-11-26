# Phase 1 Report: Core on Fakes

Status: **Phase 1 is complete and ready for your review.** I'll wait for your go-ahead before starting Phase 2.

## How to run it yourself

```sh
pip install -e '.[dev]'        # pytest and ruff only; the factory has no runtime dependencies
python3 -m pytest -q           # 109 tests plus 36 subtests, about 40 s, network guard on
ruff check factory tests

factory config validate                         # sim profile (the default)
factory config validate --profile prod          # go-live checklist: every value still empty
factory config show --agent docs-bot            # effective merged config, secrets redacted
factory features --agent docs-bot               # state, source (default/file/agent/env/cli), dependencies
factory agents                                  # filters and overlaps
factory scenario run tests/scenarios/cases/happy_path.toml
factory run --agent frontend-bot --disable review --set limits.max_attempts=3 --dry-run
```

Use `python3 -m factory.cli.main` in place of `factory` if the package isn't installed.

## What works

The full pipeline runs end to end against fakes in the `sim` profile:
- Config: layered, strictly validated, with semantic checks and secret references (`factory/config/`).
- Feature toggles: dependency errors, and a source shown for every value.
- Plugin registry: module paths in config.
- Container: builds only the adapters that enabled features need.
- Claim: lease written, then re-read and confirmed. Expired leases are reclaimed.
- Readiness checklist: one shared file, `factory/skills/readiness.toml`.
- Triage, and spec with human approval (approve with the label; the spec is posted and a human is notified).
- Clone with submodules, then the access-policy scan, then an isolated workspace with history replaced.
- Implement: any harness, through the loopback gateway.
- Mechanical gates, then repo lint and test commands, then verify evidence, then review.
- Output: push the branch, and either open an MR (merge_request mode) or post the summary to Jira (branch_only mode).
- Jira status comments throughout: by milestone, updated in place, idempotent and rate limited.
- A trace for every run.

**Bring your own harness and model work end to end.** A real subprocess "CLI agent" (`tests/scenarios/cases/fixtures/fake_agent.py`) calls the gateway in both the OpenAI-chat and Anthropic-messages formats, with streaming. It confirms from inside the sandbox that it can't see restricted files, restricted git history or factory environment variables, and its change goes through the same gates and the same MR path as everything else.

**No-proprietary-files base skill.** The mechanical layers are in place:
- workspace scan, in strip or deny_read mode;
- a guard on every builtin tool call: path, `..`, symlink, content marking, `git show`/`log -p`/`cat-file`, inline shell code;
- output filtering;
- attachment checks;
- a denial audit that stops the run after 5 attempts;
- a diff gate covering both paths and content;
- blocking tickets that depend on restricted files, with the comment naming paths only;
- a redaction backstop on traces.

`access_policy` can only be changed in `config/base.toml`. Profiles, environment variables, the CLI, agent files and repo rules are all rejected, and the test session refuses to start if the policy is off.

**Two agents side by side.** `frontend-bot` (merge_request mode) and `docs-bot` (branch_only, with spec off) each claim only their own tickets. Overlapping filters produce a warning, and the lease prevents a double claim.

## CLI agent harnesses: Claude Code, Codex, Cline, and jam

`harness.cli_agent` (`factory/core/harness/cli_agents.py`) runs a known CLI agent in the sandbox on the isolated workspace. It can launch the agent directly (`claude ...`) or through jam (`jam claude ...`, `jam codex ...`). `config/base.toml` ships five ready-made harnesses: `claude`, `codex`, `cline`, `jam-claude` and `jam-codex`. You pick one per stage and per agent, for example `[models.stages.implement] harness = "jam-claude"`.

| Preset | Invocation | Model route | Final result read from |
|---|---|---|---|
| claude | `claude -p --output-format json --max-turns N --model M --dangerously-skip-permissions`, prompt on stdin | `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` set to the gateway (Anthropic dialect); non-essential traffic, auto-update and telemetry disabled | the `result` field of the JSON output |
| codex | `codex exec --full-auto --skip-git-repo-check --model M -c model_provider="factory" ... --output-last-message F -`, prompt on stdin | a `factory` provider defined with `-c` flags pointing at the gateway (`wire_api = "chat"`), plus `OPENAI_BASE_URL` | the last-message file |
| cline | `cline --yolo --model M "<prompt>"` | `OPENAI_BASE_URL` / `OPENAI_API_KEY` set to the gateway | stdout |

- **jam:** `launcher = "jam"` turns the command into `jam <name> <same arguments>`. `jam_executable` and `jam_name` are configurable, and any agent can use jam.
- **Reporting status:** each agent is told to end its final message with a one-line JSON status (`done`, `needs_info`, `blocked` or `failed`, plus a summary and questions). The factory parses that line. If there isn't one, exit code 0 means done.
- **Settings checked at startup:** an unknown agent, a bad launcher, or a gateway dialect the agent needs but isn't enabled all fail `config validate`. A missing binary fails the ticket with "jam is not installed in the sandbox" (or the agent's binary name).
- **Overrides without code changes:** if a tool's flags differ on the box, `settings.args` replaces the preset arguments, and `extra_args` and `env` add to them.
- **What the tests prove:** fake `claude`, `codex`, `cline` and `jam` executables (`tests/scenarios/cases/fixtures/`) check the exact flags and environment each preset passes. They call the gateway in the right dialect, apply the edit, and confirm from inside the sandbox that they can't see restricted files, restricted history or leaked environment variables. Each preset produces a gated MR, directly and through jam. The real binaries were not run.
- **To confirm on the box:**
  - The **Cline** CLI flags are my best understanding and need checking.
  - **Codex** must accept `-c` provider overrides and `-` for a prompt on stdin.
  - The tools may try to reach other hosts (update checks, telemetry). The `local_dir` sandbox does not block that; a network-isolated sandbox plugin would.

## Test results

- 117 tests and 36 subtests pass; ruff reports no issues.
- 47 declarative scenarios in `tests/scenarios/cases/`, one TOML file each, covering every §12 scenario that belongs to Phase 1:
  - **Pipeline:** happy path; not-ready ticket bounced; claim race; lease reclaim; spend cap exceeded; protected paths; disabled features never instantiated; two agents; overlapping filters; labels changing between listing and claiming.
  - **Config and output modes:** per-agent override; intake routing; intake dedupe and idempotency; branch_only; merge_request; mr.* features rejected in branch_only; read_replies requiring post; jira_comments.post off; empty-commenter warning.
  - **Needs-info and run controls:** needs-info pause; dry run.
  - **Traces:** S3 without an endpoint; S3 without encryption; upload failure queued for retry; secret redaction.
  - **Submodules:** nested and relative-URL submodules at their recorded commits; a submodule on another host through a URL rewrite and a credential; a missing credential; an unreachable submodule; a refused submodule modification.
  - **Access policy:** isolation (by path, content header, symlink, directory, inside a submodule, in git history, in attachments); every read route plus repeated denials stopping the run; dependence on a restricted file; attempts to disable the policy from the CLI, the environment, a profile, an agent file and repo rules.
  - **Bring your own harness:** both gateway dialects.
- **Contract suites** (`tests/contracts/`): TicketSystem (fake); GitHost (fake on real local git, covering clone, pull, divergence and every submodule case); TraceStore (memory and local_fs backends).
- **Unit suites:** config layering and precedence, guard in both modes, gateway in both dialects including auth and budget, CLI, network guard, and deny_read combined with an external harness.

## Deferred (as planned, or newly found)

- **Phase 2:**
  - Reading Jira replies: resume, stall timeout, allowed-commenter enforcement, ignoring the agent's own comments and other bots'. The needs-info **post and pause** is done.
  - The MR comment loop: respond, auto-fix, `/apply`, fix-round limits.
  - Reviewer suggested-change blocks, and an architecture diagram generated from code.
  - Notifications beyond spec review.
- **Phase 3:** email and GitLab-comment intake adapters, draft-only override tracking, and email prompt-injection scenarios. The intake service they plug into is built.
- **Phase 4:** monitoring, metrics export, the S3 trace backend with a fake S3, grading and self-improvement. Config validation for S3 is already done.
- **Gates not yet mechanical:** coverage on changed lines and mutation score. Both need a report format (LCOV, Cobertura or JUnit) declared in the repo rules, so I'll add them with the Phase 2 verify work. The same gap applies to this codebase's own quality bar: there is no mutation testing of the factory yet.
- **Lease renewal during long runs:** a run longer than `lease_minutes` could be reclaimed by another worker. I'll add renewal when the polling loop lands in Phase 2.

## Design changes made during Phase 1

- `AgentHarness.run(task, runtime)`: the runtime lends the harness a sandboxed command runner and the per-run guard.
- `TicketSystem` gained `get_attachment`.
- New config keys:
  - `git.default_branch` and `git.branches` for per-repo base branches;
  - `workflow.priority_order`;
  - `limits.max_ticket_chars` and `limits.max_acceptance_criteria` (these define "bounded scope" for readiness);
  - `limits.max_denied_access_attempts`;
  - `gates.forbid_new_doc_files`, `gates.doc_file_patterns` and `gates.test_file_patterns`.
- **Repo rules file:** `.factory/rules.toml` in each repo holds the commands (setup, lint, test, verify, as argv lists), standards files, evidence globs, extra deny patterns and extra protected paths. It's language-agnostic, and it is always a protected path, so an agent can't edit its own checks.
- **Comment redaction:** comments scrub secrets but may name restricted *paths*, because the spec wants blocked comments to list them. Traces also redact any line that mentions a restricted marking.

## Needs your decision

1. **Status for bounced tickets.** A not-ready ticket currently moves to the `needs_info` status and gets the needs-info label. Should a bounce use a separate status (or stay in place with only a label)?
2. **Spec approval.** Approval is a label (`labels.spec_approved`). Is a label right, or should it be a Jira transition?
3. **Overlap warning.** `frontend-bot` and `docs-bot` really can overlap (a ticket could carry both ready labels), so validation warns. Add each agent's ready label to the other's `labels_none`, or accept the warning?
4. **Repo rules location.** `.factory/rules.toml` is my choice of path. Do you have an existing convention?
