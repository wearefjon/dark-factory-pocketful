# FACTORY.md

## Team

**FJoN** — WeAreDevelopers x BAND Dark Factory Hackathon, track: pocketful

## Seats

| Seat | Handle | Harness | Model | Role |
|---|---|---|---|---|
| coordinator | @odolehappy/coordinator | OpenCode | deepseek-v4-flash | Receives the stage task, dispatches to implementer, routes reviewer feedback, and delivers the final outcome. Does not write code. |
| implementer | @odolehappy/implementer | OpenCode | deepseek-v4-flash | Receives scoped tasks from coordinator, writes all service code in the result repository, runs harness checks, commits, and hands off to reviewer. |
| reviewer | @odolehappy/reviewer | OpenCode | deepseek-v4-flash | Independently inspects the committed implementation, runs harness checks from scratch, accepts or rejects the work, and reports findings to coordinator. Does not fix code. |

All three seats run on **OpenCode v2.0.22** connected to **AgentRouter** (OpenAI-compatible inference endpoint) using **deepseek-v4-flash**.

## Design rationale

**Why three seats?**
A two-seat factory collapses the implement/review boundary — the same reasoning that produced the code also evaluates it. A third, independent seat running the same checks catches gaps the implementer normalised away. The coordinator keeps the handoff protocol strict so neither seat can shortcut the loop.

**Why OpenCode?**
OpenCode provides an Agent Client Protocol (ACP) server that BAND Desktop connects to natively. It runs in a persistent background process, survives network blips, and handles the BAND room message protocol without custom tooling.

**Why deepseek-v4-flash?**
Fast enough to turn around a large spec in reasonable time, cheap enough to iterate through fix cycles without burning credits, and capable of reading and writing production-quality Python.

**Why Python + FastAPI + SQLite?**
The spec is language-agnostic. FastAPI gives typed request/response models that map directly to the spec's JSON shapes. SQLite in WAL mode handles up to 50 concurrent readers with a single writer lock — sufficient for the harness load. Everything fits in one Alpine container with no external services.

## How the factory catches and recovers from bad work

1. **Implementer self-check**: Before any handoff, the implementer runs the full harness suite and includes the results in its message to the reviewer. Work that does not pass is not handed off — the implementer fixes it first.

2. **Independent reviewer check**: The reviewer runs the harness independently from the committed revision, not from the implementer's reported output. It also reads the diff adversarially, looking for correctness issues the harness does not cover.

3. **Rejection and rework**: If the reviewer finds failures, it sends a structured report to the coordinator listing each failure with the exact error, the relevant code path, and a suggested fix direction. The coordinator routes this back to the implementer with full context. The implementer must produce a new commit — it may not amend or rebase the existing one.

4. **Coordinator acceptance gate**: The coordinator accepts only a revision that the reviewer has explicitly approved in writing. It never accepts the implementer's own assessment of correctness.

5. **Escalation**: If the implementer cannot resolve a reviewer's objection after two cycles, the coordinator logs the concrete blocker and reports it as the stage outcome without asking the human for input.

## Costs

| Item | Value |
|---|---|
| Provider | AgentRouter |
| Model | deepseek-v4-flash |
| Harness | OpenCode v2.0.22 |
| Estimated token spend (toy run) | ~5K tokens |
| Estimated token spend (pocketful stage 1) | ~80K–150K tokens |

## How to stand up this factory

1. Install OpenCode: `npm install -g @opencode/cli`
2. Connect AgentRouter: `opencode auth login` → select AgentRouter → enter API key
3. Install BAND Desktop and sign in
4. In BAND Desktop → Agents → create coordinator, implementer, reviewer using the mandate files in `mandates/`
5. Create a room, add all three agents
6. Send the stage task to `@<your-handle>/coordinator` and do not send further messages

## Stages completed

| Stage | Harness result |
|---|---|
| 1 | pass |
