# How to use the stage skills in the factory

The skills below come from other teams and were written for interactive sessions. In the factory, follow them with these adaptations. The mandatory base skills always win on any conflict.

- **No one is waiting for you.** Where a skill says to ask the user, pause for confirmation, wait for feedback, or hand control to a human, stop and report `needs_info` with specific numbered questions instead. Do not wait.
- **Working notes go outside the repo.** Where a skill says to write research, plans, outlines, specs, or other documents (for example under `.humanlayer/tasks/`, `thoughts/`, or `docs/`), write them under `$FACTORY_ARTIFACTS` if that directory is given, and otherwise keep them in your final summary. Never add them to the repository unless the ticket asks for documentation.
- **The factory owns every external action.** Do not commit, push, open or describe pull requests, comment on tickets, publish to an issue tracker, or touch Linear, Jira, or GitLab. Where a skill does those things, produce the content and stop.
<!-- factory:if not subagents -->
- **One session, no sub-agents.** Where a skill spawns sub-agents or parallel workers, do the work yourself in sequence. The skills below have already been rewritten for this; follow the single-session wording.
<!-- factory:end -->
<!-- factory:if not internet -->
- **Offline.** Ignore any step that needs the web, `gh`, a CDN, or an online service. The skills below have already been rewritten for this where it matters.
<!-- factory:end -->
- **Interactive helpers are not available.** Human-in-the-loop scripts, browsers, and setup wizards a skill mentions are not installed. If the work truly needs one, report `needs_info` or `blocked`.
- **Limits still apply.** The ticket's scope, the size limits, protected paths, and the access policy override anything a skill suggests.
