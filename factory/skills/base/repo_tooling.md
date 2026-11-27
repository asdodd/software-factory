# Base skill: the repo's own tooling and instructions

Precedence, lowest to highest:

1. These factory base skills: the default way to work.
2. The repo's own instructions: `AGENTS.md`, `CLAUDE.md`, `.claude/skills/`,
   `.clinerules/`, `CONTRIBUTING.md`, and similar files in each repo. Where they
   conflict with a base skill, follow the repo.
3. Never overridden by anything: the restricted-files skill, the shared process
   rules on protected paths, tests, submodules, and contacting systems, and the
   factory's mechanical gates, which check every change.

Each repo owns its quality tooling. Before you report done:

- Find how the repo lints, tests, measures coverage, and runs mutation testing:
  its CI file (`.gitlab-ci.yml` and the like), package scripts, `Makefile`,
  `tox.ini`, `pyproject.toml`, or its own instructions. Use the "Repo commands"
  below when the task lists them.
- Run what applies to your change, in that repo's directory, and fix what fails.
- Add or update tests for the code you change. Where the repo measures coverage
  or mutation score, keep your change at or above the repo's own bar.
- Do not change the tooling's configuration to make a check pass.
- If a check cannot run here (a missing service or credential), say which one in
  your summary; the repo's CI runs it on the merge request.
