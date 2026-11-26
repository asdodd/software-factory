# Base skill: work within the request budget (mandatory)

This deployment is limited by the number of model requests per hour, not by tokens. Every turn you take costs a request, so a long prompt is cheap and an extra round trip is expensive.

- Plan first, then act. Gather what you need in as few turns as possible.
- Batch independent tool calls into one turn: read several files together, run related searches together.
- Read each file once and keep what you learned. Do not re-open files to re-check them.
- Make complete edits. Prefer one well-considered change per file over many small ones.
- Run the tests once per meaningful change, not after every line.
- Stay inside what the ticket needs. Do not explore unrelated code.
- When you are done, stop. Do not summarize repeatedly or ask whether to continue.

<!-- factory:if not subagents -->
**Do not spawn sub-agents.** Never start sub-agents, background agents, parallel workers, or the Task tool, even if a skill below describes doing so. Each one multiplies the request count. Do that work yourself, in this session, one step after another.
<!-- factory:else -->
**Sub-agents are allowed but cost requests.** Use one only when it clearly saves more turns than it spends: one sub-agent for a large, separable search, not one per question. Never more than two at a time.
<!-- factory:end -->

<!-- factory:if not internet -->
**There is no internet access.** Do not browse, search the web, fetch URLs, call online APIs, or install packages from the internet; such attempts fail and waste requests. Use what is in the workspace. If the work truly needs something online, report `blocked` and say what.
<!-- factory:end -->
