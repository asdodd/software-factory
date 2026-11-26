# Base skill: no proprietary files (mandatory)

You must never read, open, search, print, summarize, copy, diff, or otherwise
ingest any file whose path or name contains a restricted marking (for example
"proprietary"), or whose content carries one. This applies to every repo,
submodule, ticket attachment, email attachment, and command output, including
`cat`, `grep`, `git show`, `git diff`, and `git log -p`.

Restricted files have already been removed from or hidden in your workspace.
Do not try to find them. If the task cannot be done without a restricted file,
stop and report `blocked`, naming the paths only. Never quote or describe
their content.

Attempts are blocked mechanically and audited. Repeated attempts stop the run.
