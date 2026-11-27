# TODO

Everything in the spec's five phases is built and tested against simulators. This is what's left: decisions only you can make, work needed to go live, and known gaps. Items are roughly in order of priority within each section.

## Decisions needed

Each of these has a working default today; it's a config change or a small code change once you decide.

- [ ] **Jira version.** Comment idempotency uses comment properties, documented for Jira Server/Data Center 7.x and later. If your version ignores them, switch to a body marker.
- [ ] **GitLab follow-up project.** Keep one `intake.gitlab.default_project`, or map projects by GitLab group? (Ties in with the multi-repo intake suggestions below.)
- [ ] **Multi-repo intake.** Pick from the suggestions below.
- [ ] **Defaults chosen while you were offline:**
  - draft-only monitoring intake;
  - grader calibration needs 0.8 agreement with a tolerance of 1;
  - self-improvement may edit only `factory/skills/**` and `config/agents/**`;
  - the §8.3 limits and polling defaults.

## Decided

- [x] **Mail sender authentication.** The `From:` header is trusted (`require_authenticated_sender = false`). Duplicates go to the first ticket, with one reply and a "Related report" comment:
  - the same Message-ID, compared without brackets or case;
  - the same thread;
  - the same request in a new thread, sent again or forwarded by someone else. "Same" means the same subject without `Re:`/`Fwd:` and the same body without quote markers or forward headers, within `intake.dedupe_window_days`.
- [x] **Email to Jira user mapping.** The reporter is the Jira user found by the sender's address (`user/search`, exact and active match only, cached). `user_map` is gone. A sender with no Jira account is named in the description, and the factory is the reporter.
- [x] **Status model.** Humans own the statuses (Open, In Progress, In Review, Complete). The factory claims from the ready status and only moves a ticket to In Progress, when implementation starts. Only `workflow.statuses.ready` and `in_progress` remain.
- [x] **Bounce status.** Label only (`labels.needs_info`). The status is untouched; a human removes the label once the ticket is fixed.
- [x] **Spec approval.** Kept as the `labels.spec_approved` label. A posted spec gets `labels.spec_review` while it waits.
- [x] **Stall status.** The ticket stays In Progress and gets `labels.stalled`.
- [x] **Labels in config.** Every label is in `[labels]`, with defaults in `config/base.toml`: ready, claimed, needs_info, spec_review, spec_approved, in_review, blocked, failed, stalled. Any value may contain `{agent}`.
- [x] **Overlapping agent filters.** Each agent's lock label (`ai-claimed-{agent}`) keeps two agents off the same ticket. An agent that sees another agent's lock after adding its own backs off before taking a lease, so at most one wins. Tickets with any state label are never claimed as new work.
- [x] **Repo rules location.** `.factory/rules.toml` is gone, so nothing is added to target repos. Optional per-repo settings live in the factory's config under `[repos."group/name"]`.
- [x] **Change requests with auto-fix off.** The agent always replies in the thread that a human will make the change (even with `mr.respond` off), notes it on the Jira ticket, and counts it (`mr_changes_left_for_human`).
- [x] **Missing coverage or mutation setup.** Repos own their coverage, CI and mutation tooling, and the factory no longer notes "not measured". A base skill tells the agent to find and run the repo's own tooling. Factory skills are the base; each repo's `AGENTS.md`, `CLAUDE.md` and skills override them, except the restricted-files skill, the process rules and the mechanical gates. A repo's CI file counts as "verification known" for readiness.

## Multi-repo intake: suggestions

A bug often needs the app, the product quality repo and the shared config repo. A ticket can already name several repos in its `repo` field. Each repo that changes gets its own branch and MR, and untouched repos are skipped. What's missing is filling that field in well at intake, and handling the MRs as a set.

1. **Repo sets by issue type or component (recommended first).** Config such as `[intake.repo_sets] Bug = ["web/app", "qa/product-quality", "platform/shared-config"]`. Intake puts the whole set in `repo`, and the agent changes only what the ticket needs. Deterministic and cheap.
2. **Let the intake drafter choose several repos.** The draft prompt returns `repos: [...]`, limited to known repos and merged with the repo set. This catches tickets that need only the app plus config.
3. **One agent per product, not per repo.** An agent's `repos` must cover every repo a ticket names, or the ticket bounces as out of scope. Give the agent that handles the app all three repos.
4. **A base skill for bugs.** For example: "For a bug, add a regression test in the product quality repo, and change shared config only through existing keys." It lives in `factory/skills/base`, so no repo changes.
5. **Merge order and linking.** The shared config MR usually has to merge first. Add a per-repo `merge_order` and a "merge after" line in each MR description. Later, a label or merge train can merge the set together (see Known gaps).
6. **Readiness for bugs.** Optionally require that a bug ticket names the product quality repo, so a regression test is part of the change.

## To go live

See [design/GO_LIVE.md](design/GO_LIVE.md) for the exact keys.

- [ ] Fill in `config/profiles/prod.toml` and `staging.toml`, until `factory config validate --profile prod` passes.
- [ ] Create the Jira custom fields: `lease_owner`, `lease_expires`, `repo`, `acceptance_criteria`, `verify_steps`, `sponsor_team`, `source_ref`, `fingerprint`. Then map their ids in `[workflow.fields]`.
- [ ] Issue service accounts and tokens: Jira, GitLab, the model endpoint, S3 write-only and S3 read-only credentials.
- [ ] Write real agent definitions in `config/agents/` (the shipped ones are `SIM` examples), and fill in `allowed_commenters` and `[notifications]`.
- [ ] Optional: add `[repos."group/name"]` entries to the factory config for repos whose commands the factory should re-run as gates (lint, test, verify, standards, evidence, coverage and mutation reports). Nothing is added to the target repos.
- [ ] Create the labels in `[labels]` if your Jira restricts labels, and give people a board filter on the state labels.
- [ ] Prepare the S3 bucket: versioning or Object Lock, lifecycle expiry per classification rules, a policy that denies unencrypted puts, and no delete rights for either credential.
- [ ] In GitLab, protect branches and add code owners for lint config, test config and CI files. CI must use the target branch's configuration.
- [ ] Install `git` 2.20 or later on the worker box, and `claude`, `codex`, `cline` and `jam` if those harnesses are used.
- [ ] Run against staging with `dry_run = true` first, then turn off dry run on one low-risk agent.
- [ ] Label 20 to 50 runs by hand and run `factory grade --calibrate` before turning on `feedback.self_improvement`.
- [ ] Run the services: `factory run --all-agents --loop` and `factory intake --loop`. Schedule `factory grade` and `factory improve`.

## Verify on the real box

These were tested only with fakes or emulators.

- [ ] **Cline CLI flags** (`--yolo`, `--model`, prompt as an argument). Override with `harnesses.cline.settings.args` if they differ.
- [ ] **Codex:** accepts `-c model_providers.*` overrides and reads the prompt from stdin with `-`.
- [ ] **Claude Code:** honors `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` against the gateway, and `--dangerously-skip-permissions` is acceptable inside the sandbox.
- [ ] **Claude Code and the agent kit:** with `-p --dangerously-skip-permissions`, the workspace's `.claude/settings.json` still runs the `PreToolUse` hook and enforces the `permissions.deny` rules, the character-class deny patterns match as expected, and the `restricted-files` skill loads.
- [ ] **Cline hooks:** the hook file location (`.clinerules/hooks/PreToolUse`), its JSON input (`preToolUse.toolName`, `parameters`, `workspaceRoots`) and output (`cancel`, `errorMessage`) match the installed Cline. The hook was tested only against that assumed format.
- [ ] **Codex:** whether the installed version supports pre-tool-use hooks or reads skills from a repo directory. Today Codex gets the rule through `AGENTS.md` only, and `factory config validate` warns about it.
- [ ] **jam:** passes arguments through unchanged (`jam claude <args>`).
- [ ] **Jira REST v2 and GitLab REST v4 response shapes,** against the real versions. Each adapter passes its contract suite on hand-written emulators only.
- [ ] **The S3-compatible endpoint:** path-style addressing, SSE-KMS support, and `If-None-Match` on put.

## Known gaps

- [ ] **Sandbox isolation.** `local_dir` doesn't isolate the network or the filesystem view, so CLI agents could reach other hosts or read outside the workspace. Add a `bubblewrap` or rootless `podman` sandbox plugin: no network except the gateway socket, and the workspace as the only writable view.
- [ ] **Restricted files are enforced by the agents only.** The factory no longer filters, strips or gates restricted files: the agent kit's skill and hooks do. The hook is best-effort for shell commands (it reads paths out of the command line), and an agent without hooks (Codex, or an unknown `harness.command`) relies on the skill alone. Factory-side model calls that are not agents (triage, spec, review, intake drafting) see ticket text, attachments, standards files and diffs as they are.
- [ ] **Multi-repo tickets.** Each changed repo gets its own branch and MR with the same branch name; they are not linked to each other on the git host (only in each MR description and the Jira comment). MR comment fixes work per MR. Decide whether cross-repo changes should be merged together (for example, by a label or a merge train).
- [ ] **Jira project roles.** These aren't resolved, so commenter rules that use `project_roles` won't match yet. Users and groups work.
- [ ] **Reviewing human-authored MRs.** The reviewer only reviews the agent's own MRs. If wanted, add a reviewer agent for people's MRs; `/apply` already opens follow-up MRs instead of pushing to their branch.
- [ ] **Hook decisions in traces.** The kit's hook blocks calls but does not report them to the factory, so blocked attempts are not in run traces or metrics. It could append to a log file under `.factory/` that the worker reads after the run.
- [ ] **Mutation testing of the factory itself.** The factory enforces mutation gates on target repos, but its own codebase has no mutation run yet. Add one (for example `mutmut`) to the factory repo's CI.
- [ ] **Partial trace flushing.** Traces are uploaded at the end of each run. Add a periodic flush (`trace_store.flush_interval_seconds`) for very long runs.
- [ ] **Metrics export.** `factory metrics --format prometheus` prints text. Wire it to a node-exporter textfile or a push target if you use Prometheus.
- [ ] **Concurrency.** Workers are sequential per agent (`claim.max_in_flight` tickets per cycle). Run several worker processes per agent for throughput; leases already make that safe.
- [ ] **PlantUML.** The diagrams render with the distribution's PlantUML 1.2020. Upgrade it if you want newer syntax.
