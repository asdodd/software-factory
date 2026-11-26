# Design

The diagrams are PlantUML sources (`*.puml`), with rendered PNGs in `img/`. To re-render after editing a source:

```sh
plantuml -tpng -o img design/*.puml
```

## End-to-end flow ([flow.puml](flow.puml))

![End-to-end flow](img/flow.png)

## Ports and adapters ([components.puml](components.puml))

![Ports and adapters](img/components.png)

## Port interfaces ([ports.puml](ports.puml))

![Port interfaces](img/ports.png)

## Logical ticket states ([workflow-states.puml](workflow-states.puml))

![Logical ticket states](img/workflow-states.png)

## Collaboration loops ([collaboration.puml](collaboration.puml))

![Collaboration loops](img/collaboration.png)

## Documents

| Document | Contents |
|---|---|
| [PHASE0_REPORT.md](PHASE0_REPORT.md) | Design decisions, the feature table, the Jira mapping format, the open questions |
| [PHASE1_REPORT.md](PHASE1_REPORT.md) | The core on fakes, the access policy, harnesses (including Claude Code, Codex, Cline and jam) |
| [PHASE2_REPORT.md](PHASE2_REPORT.md) | The Jira reply loop, the MR comment loop, reviewer suggestions, the coverage and mutation gates |
| [PHASE3_REPORT.md](PHASE3_REPORT.md) | Email and GitLab-comment intake, draft-only mode, override tracking |
| [PHASE4_REPORT.md](PHASE4_REPORT.md) | Monitoring, metrics, S3 traces, grading, self-improvement |
| [PHASE5_REPORT.md](PHASE5_REPORT.md) | Real adapters tested against fixtures |
| [GO_LIVE.md](GO_LIVE.md) | The config keys and secrets to fill in, and the infrastructure checklist |
