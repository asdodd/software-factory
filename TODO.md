# TODO

Everything in the spec's five phases is built and tested against simulators. This is what's left: decisions only you can make, work needed to go live, and known gaps. Items are roughly in order of priority within each section.

## Decisions needed

Each of these has a working default today; it's a config change or a small code change once you decide.

- [ ] **Jira version.** Comment idempotency uses comment properties, documented for Jira Server/Data Center 7.x and later. If your version ignores them, switch to a body marker.
- [ ] **Mail sender authentication.** Does the internal mail server add `Authentication-Results` (DKIM or DMARC)? If yes, set `intake.email.require_authenticated_sender = true`. Without it, the allowlist trusts the `From:` header.
- [ ] **Email to Jira user mapping.** Keep `intake.email.user_map` maintained by hand, or look users up by email in Jira (one extra API call per email)?
- [ ] **GitLab follow-up project.** Keep one `intake.gitlab.default_project`, or map projects by GitLab group?
- [ ] **Bounce status.** A not-ready ticket currently moves to `needs_info` and gets the needs-info label. Use a separate status, or a label only?
- [ ] **Spec approval.** Approval is the `labels.spec_approved` label today. Keep the label, or make it a Jira transition?
- [ ] **Stall status.** Stalled tickets go to `workflow.statuses.stalled` (`Open` in sim). Confirm.
- [ ] **Overlapping agent filters.** `frontend-bot` and `docs-bot` can both match a ticket carrying both ready labels. Add each agent's ready label to the other's `labels_none`, or accept the warning?
- [ ] **Repo rules location.** `.factory/rules.toml` in each repo. Keep it, or use an existing convention?
- [ ] **Change requests with auto-fix off.** Today the agent replies that a human will make the change. Should it stay silent instead?
- [ ] **Missing coverage or mutation setup.** Today it's a note in the MR. Should it fail the gate for some repos?
- [ ] **Defaults chosen while you were offline:**
  - draft-only monitoring intake;
  - grader calibration needs 0.8 agreement with a tolerance of 1;
  - self-improvement may edit only `factory/skills/**` and `config/agents/**`;
  - the §8.3 limits and polling defaults.

## To go live

See [design/GO_LIVE.md](design/GO_LIVE.md) for the exact keys.

- [ ] Fill in `config/profiles/prod.toml` and `staging.toml`, until `factory config validate --profile prod` passes.
- [ ] Create the Jira custom fields: `lease_owner`, `lease_expires`, `repo`, `acceptance_criteria`, `verify_steps`, `sponsor_team`, `source_ref`, `fingerprint`. Then map their ids in `[workflow.fields]`.
- [ ] Issue service accounts and tokens: Jira, GitLab, the model endpoint, S3 write-only and S3 read-only credentials.
- [ ] Write real agent definitions in `config/agents/` (the shipped ones are `SIM` examples), and fill in `allowed_commenters` and `[notifications]`.
- [ ] Add a `.factory/rules.toml` to each target repo: lint, test and verify commands, standards files, evidence globs, and optionally coverage and mutation reports.
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
