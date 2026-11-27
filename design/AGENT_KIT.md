# The agent kit, multi-repo tickets, and local workspaces

## Restricted files are the agents' job

The factory does not filter, strip, hide or gate restricted files. Agents
enforce the rule themselves with their own skills and tools. The rule and its
marker word are written in the `restricted-files` skill.

The kit lives in `factory/agent_kit/` and has these files:

| File | What it does |
|---|---|
| `.claude/skills/restricted-files/SKILL.md` | The skill. Claude Code loads it natively. |
| `.claude/hooks/restricted_files.py` | A pre-tool-use hook (stdlib Python, no factory import). It blocks reads, searches, edits, and overwrites of files whose path or content carries the marker. It also blocks shell commands that name such files or search over them. It prints paths only, never content, and it fails closed. |
| `.claude/settings.json` | Wires the hook for `Read`, `Edit`, `Write`, `Grep`, `Glob`, `Bash` and the notebook tools, and adds `permissions.deny` rules for marked paths. |
| `.clinerules/hooks/PreToolUse` | Runs the same hook for Cline (`--format cline`). |
| `AGENTS.md`, `CLAUDE.md` | The rule in brief, for Codex, Cline and others. When these files already exist, the rule is appended to them. |

A few details:

- **Sentinels.** Text between `agent-kit:restricted-files:begin` and `:end` is
  ignored by the hook, so the kit's own description of the rule never blocks
  searches of a workspace. The marker is matched in either case.
- **Adding markers.** `RESTRICTED_MARKERS="a,b"` adds markers. It can never
  remove the built-in one.
- **Searches.** A blocked search names the restricted paths, so the agent can
  narrow its search.
- **Coverage.** The hook is a backstop, not a sandbox. For shell commands it
  reads paths out of the command line, so it can miss paths a command builds
  at run time. Agents without hook support rely on the skill alone, and
  `factory config validate` warns about them. Today that means Codex and any
  `harness.command` without `runs_kit_hooks = true`.

### In a factory run

1. The worker clones the repos and makes the isolated copy.
2. It installs the kit before the baseline commit (one repo) or at the top of
   the workspace (several repos), so the kit never appears in a diff or on a
   pushed branch.
3. After the agent finishes, the worker compares the kit files with what it
   installed. If the agent changed or removed any of them, the run is
   **blocked** and nothing is pushed.

Restricted files stay in the workspace and on the branch.

The built-in tool-loop harness is gone. The default implement harness is
`claude`, and `codex`, `cline`, `jam-*` and `harness.command` remain.

## Multi-repo tickets

The ticket's `repo` field may name several repos, separated by commas,
whitespace or new lines (a list field also works). The first one is the
primary. Each must be in the agent's `repos`.

- **Layout.** Every repo is cloned. In the workspace each repo is a directory
  named after its last path segment (`group__name` when two repos share a
  name). The agent works from the parent directory, and the instructions list
  each repo's directory and commands.
- **Gates.** Mechanical gates and each repo's own lint, test, quality and
  verify commands run per changed repo, with failures prefixed by the repo.
  Size limits apply to the ticket as a whole.
- **Publishing.** Every changed repo gets the same branch name and its own MR,
  which names the other repos. A repo the agent left alone gets nothing. The
  Jira comment links every MR.
- **After publishing.** Each MR is watched for comments on its own. Fixes and
  `/apply` work per MR.

A one-repo ticket works exactly as before: the repo copy is the working
directory.

## Local workspaces

Use the kit and your own agent without the pipeline or intake.

- This repo is a workspace as cloned: the kit is at the root, and `repos/`,
  `tickets/` and `workspace.toml` are git-ignored.
- `factory workspace init DIR` makes another workspace anywhere.

```sh
factory workspace add git@gitlab.example.com:web/portal.git   # list it and clone it into repos/portal
factory workspace add git@gitlab.example.com:web/design-system.git --branch develop
factory workspace sync                                        # clone missing repos, fast-forward the rest
factory workspace list
factory workspace ticket SIM-123                              # optional: tickets/SIM-123/TICKET.md + the ticket's repos
```

- **Where to start the agent.** Start it at the workspace root so the kit
  applies everywhere.
- **Cloned repos.** Each cloned repo also gets the kit's files where it has
  none of its own. They are listed in the repo's `.git/info/exclude`, so they
  never show up in `git status`.
- **Repos with their own settings.** If a repo has its own
  `.claude/settings.json`, the hook applies only when the agent starts at the
  workspace root, and `sync` says so.
- **Pulling in a ticket.** `factory workspace ticket` uses the configured
  ticket system (`--profile`, `--config-dir`). It needs
  `workspace.git_url_template` (for example `git@gitlab.example.com:{path}.git`)
  to turn the ticket's repo paths into clone URLs.
- **Credentials.** Git runs as you, with your own credentials.
