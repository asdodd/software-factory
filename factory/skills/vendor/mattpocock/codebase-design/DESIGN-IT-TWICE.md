<!-- Modified by software-factory: passages that need sub-agents or internet access are wrapped in factory:if blocks with single-session and offline alternatives. Upstream text is otherwise unchanged; see factory/skills/vendor/SOURCES.toml for the source commit. -->

# Design It Twice

<!-- factory:if subagents -->
When the user wants to explore alternative interfaces for a chosen deepening candidate, use this parallel sub-agent pattern. Based on "Design It Twice" (Ousterhout): your first idea is unlikely to be the best.
<!-- factory:else -->
When the user wants to explore alternative interfaces for a chosen deepening candidate, use this pattern, producing each design yourself in turn. Based on "Design It Twice" (Ousterhout): your first idea is unlikely to be the best.
<!-- factory:end -->

Uses the vocabulary in [SKILL.md](SKILL.md): **module**, **interface**, **seam**, **adapter**, **leverage**.

## Process

### 1. Frame the problem space

<!-- factory:if subagents -->
Before spawning sub-agents, write a user-facing explanation of the problem space for the chosen candidate:
<!-- factory:else -->
Before designing, write an explanation of the problem space for the chosen candidate:
<!-- factory:end -->

- The constraints any new interface would need to satisfy
- The dependencies it would rely on, and which category they fall into (see [DEEPENING.md](DEEPENING.md))
- A rough illustrative code sketch to ground the constraints, not a proposal, just a way to make the constraints concrete

<!-- factory:if subagents -->
Show this to the user, then immediately proceed to Step 2. The user reads and thinks while the sub-agents work in parallel.
<!-- factory:else -->
Then immediately proceed to Step 2.
<!-- factory:end -->

<!-- factory:if subagents -->
### 2. Spawn sub-agents

Spawn 3+ sub-agents in parallel, each with a fresh context window. Each must produce a **radically different** interface for the deepened module.

Prompt each sub-agent with a separate technical brief (file paths, coupling details, dependency category from [DEEPENING.md](DEEPENING.md), what sits behind the seam). The brief is independent of the user-facing problem-space explanation in Step 1. Give each agent a different design constraint:

- Agent 1: "Minimize the interface: aim for 1–3 entry points max. Maximise leverage per entry point."
- Agent 2: "Maximise flexibility: support many use cases and extension."
- Agent 3: "Optimise for the most common caller: make the default case trivial."
- Agent 4 (if applicable): "Design around ports & adapters for cross-seam dependencies."

Include both [SKILL.md](SKILL.md) vocabulary and GLOSSARY.md vocabulary in the brief so each sub-agent names things consistently with the architecture language and the project's domain language.

Each sub-agent outputs:

1. Interface (types, methods, params, plus invariants, ordering, error modes)
2. Usage example showing how callers use it
3. What the implementation hides behind the seam
4. Dependency strategy and adapters (see [DEEPENING.md](DEEPENING.md))
5. Trade-offs: where leverage is high, where it's thin
<!-- factory:else -->
### 2. Produce three radically different designs yourself

Write three designs in one response, one after another, each under a different constraint, and keep each one honest to its constraint rather than converging:

- Design 1: "Minimize the interface: aim for 1–3 entry points max. Maximise leverage per entry point."
- Design 2: "Maximise flexibility: support many use cases and extension."
- Design 3: "Optimise for the most common caller: make the default case trivial."
- Design 4 (if applicable): "Design around ports & adapters for cross-seam dependencies."

Use the [SKILL.md](SKILL.md) vocabulary and GLOSSARY.md vocabulary so each design names things consistently.

Each design gives:

1. Interface (types, methods, params, plus invariants, ordering, error modes)
2. Usage example showing how callers use it
3. What the implementation hides behind the seam
4. Dependency strategy and adapters (see [DEEPENING.md](DEEPENING.md))
5. Trade-offs: where leverage is high, where it's thin
<!-- factory:end -->

### 3. Present and compare

Present designs sequentially so the user can absorb each one, then compare them in prose. Contrast by **depth** (leverage at the interface), **locality** (where change concentrates), and **seam placement**.

After comparing, give your own recommendation: which design you think is strongest and why. If elements from different designs would combine well, propose a hybrid. Be opinionated: the user wants a strong read, not a menu.
