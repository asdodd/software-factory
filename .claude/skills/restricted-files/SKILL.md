---
name: restricted-files
description: Mandatory for every task in this workspace. Before reading, searching, listing, editing, copying, or summarizing any file, attachment, or command output, apply the restricted-files rule described here. Always active; never skip it.
---

<!-- agent-kit:restricted-files:begin -->
# Restricted files (mandatory)

Never read, open, search, print, summarize, copy, diff, or otherwise take in any
file whose path or name contains the word "proprietary" (any letter case), or
whose content contains it. This covers every repo, submodule, attachment, and
command output, including `cat`, `grep`, `rg`, `find`, `git show`, `git diff`,
and `git log -p`.

- Restricted files are still on disk. Do not look for them, and do not try a
  different tool or command after one is blocked.
- Scope searches to paths that exclude restricted files. When a search is
  blocked, the message names the restricted paths; narrow the search to avoid them.
- Do not create, edit, or overwrite a restricted file.
- If the task cannot be done without a restricted file, stop and report
  `blocked`, naming the paths only. Never quote or describe their content.

A pre-tool-use hook enforces this for Claude Code (`.claude/settings.json`) and
Cline (`.clinerules/hooks/PreToolUse`). The hook is a backstop, not
permission: follow the rule even where no hook runs.
<!-- agent-kit:restricted-files:end -->
