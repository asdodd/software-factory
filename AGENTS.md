# software-factory

This repo is two things:

1. **The factory**: a pipeline that turns ready tickets into gated merge
   requests (`factory/`, `config/`, `tests/`). See README.md.
2. **A local workspace**: the agent kit is installed at this root, so you can
   clone repos under `repos/` and work on them with Claude Code, Codex, Cline,
   or another agent, without the pipeline or intake.

## Working in the local workspace

- `factory workspace add <git-url>` lists a repo in `workspace.toml` and clones
  it into `repos/<name>/`. `factory workspace sync` clones or fast-forwards all
  of them. `factory workspace list` shows what is there.
- `factory workspace ticket <KEY>` (optional) writes a ticket's text, comments,
  and attachments to `tickets/<KEY>/` and clones the repos it names.
- `repos/`, `tickets/`, and `workspace.toml` are git-ignored here.
- Start the agent at this directory so the kit applies to every repo. Each
  cloned repo also gets the kit's files where it has none of its own.
- Use another directory instead with `factory workspace init <dir>`.

## Working on the factory itself

- `python3 -m pytest -q` runs everything against simulators (the network guard
  blocks non-local sockets). `ruff check factory tests` must pass.
- Core depends only on `factory/ports`; adapters live in `factory/adapters`.
- The agent kit's source is `factory/agent_kit/`. The copies at this root must
  stay identical to it (a unit test checks this).

<!-- agent-kit:restricted-files:begin -->
## Mandatory: restricted files

Never read, open, search, print, summarize, copy, or diff any file whose path or
content contains the word "proprietary" (any letter case). Restricted files are
still on disk: scope searches to avoid them, never work around a blocked tool
call, and if the task needs one, stop and report `blocked`, naming the path
only. The full rule is the `restricted-files` skill in `.claude/skills/`.
<!-- agent-kit:restricted-files:end -->
