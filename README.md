# Dark Factory — Pocketful

**Team:** FJoN
**Hackathon:** WeAreDevelopers x BAND — Dark Factory
**Track:** Pocketful (wallet and payments service)

## What this is

A software factory built in BAND Desktop: three OpenCode agents sharing one room,
coordinating entirely through @mention routing, building a wallet and payments service
against a written specification — with no further human input after the initial dispatch.

## How to read this repository

| Path | What it is |
|---|---|
| `FACTORY.md` | Seat setup, design rationale, costs, and how the factory catches bad work |
| `mandates/` | One mandate file per seat — the standing instructions each agent carries |
| `room.json` | Full BAND Desktop room session export, unedited |
| `stage-1/` | The wallet and payments service at stage 1, builds and runs from a clean container |

## How to run the service

See `stage-1/RUN.md` for the single command to build and start the service.

Requirements: Docker. No other dependencies on the host.

## How to verify

Install the harness from the official kickoff bundle:

```bash
git clone https://github.com/band-ai/dark-factory-wearedevs
cd dark-factory-wearedevs
python -m venv .venv && source .venv/bin/activate
pip install -r harness/requirements.txt
python -m playwright install chromium

python -m harness run --track pocketful --repo /path/to/this/repo --stage 1 --out /tmp/pf-check
```

## Factory summary

Three seats — coordinator, implementer, reviewer — each running OpenCode v2.0.22
on deepseek-v4-flash via AgentRouter. The coordinator receives the stage task and
dispatches a self-contained handoff to the implementer. The implementer writes the
service, runs harness checks, and commits before handing off to the reviewer. The
reviewer runs the same checks independently and either accepts or returns structured
feedback. The coordinator accepts only reviewer-approved revisions.

See `FACTORY.md` for full detail.
