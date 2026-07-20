# Devpost submission draft

## Project

**Name:** Qwendom Agent Society
**Track:** Track 3 — Agent Society
**One-line pitch:** Qwendom turns complex AI work into an inspectable society of
Qwen-powered specialists whose assignments, disagreements, votes, evidence,
and failures remain accountable.

## What it does

Qwendom decomposes a complex task across an Architect, Researcher, Builder, and
Critic, adding capability-matched specialists when needed without silently
expanding the voting roster. Agents discuss readiness, expose typed blockers,
challenge competing proposals, vote with reasons, execute assigned work, and
produce durable evidence for review, recap, and role dossiers. Required
acceptance evidence—not polished prose—determines the terminal state.

## Why an Agent Society

The problem is not merely generating an answer. Real work needs distinct
competencies, explicit ownership, adversarial review, conflict resolution,
recovery from worker failure, and a trace that a human can audit. A single
agent hides those boundaries. Qwendom makes them visible and durable while
keeping governance membership separate from specialist participation.

## Qwen Cloud and tools

The submission runtime uses direct Qwen Cloud through the international
DashScope endpoint with `qwen3.7-plus`. The integration includes model
capability preflight, structured-output recovery, bounded retries with
`Retry-After`, cancellation handling, and redacted provider errors. The
Researcher may autonomously choose a budgeted Context7 lookup and records tool
intent plus result provenance. No OpenRouter result is presented as Qwen proof.

## Official benchmark result

The frozen version 3 benchmark uses a composite security release audit, shared
read-only evidence tools, and 24 deterministic acceptance checks. Across three
successful trials per mode, both society and single agent scored **100.00**.
Society used **53 calls / 810,389 tokens / 233.17 s per trial** versus **3 calls
/ 8,310 tokens / 11.71 s**. No attempt was discarded. The honest conclusion is
equal quality, not an efficiency win: society demonstrates inspectable teamwork
but costs about 19.9 times more time and 97.5 times more tokens on this task.

## Significant hackathon-period work

- Reproducible Qwen single-agent versus society benchmark and saved raw results.
- Typed readiness blockers and human-decision pause/resume semantics.
- Generic acceptance-evidence contract and truthful terminal states.
- Worker error taxonomy, retries, idempotency, exhaustion, and capability-based reassignment.
- Capability registry, dynamic non-voting specialists, and explicit voter roster.
- Autonomous Context7 tool choice with durable intent and provenance.
- Qwen rate-limit handling, redaction, preflight, and provider-neutral recovery events.

## Links and acknowledgements

The placeholders below are submission-owner fields, not unfinished engineering
tasks. They remain explicit to prevent accidental submission with missing
public/account information.

- Source: https://github.com/yamedoff/qwendom
- Benchmark methodology: `docs/BENCHMARK.md`
- Architecture: `docs/HACKATHON_ARCHITECTURE.md`
- Public demo: **PENDING**
- Video: **PENDING**
- Alibaba deployment proof: **POSTPONED / PENDING**
- Team representative and members: **PENDING USER DETAILS**
- Third-party acknowledgements: `docs/THIRD_PARTY_NOTICES.md`
- Project license: **Apache-2.0**

Do not submit while any `PENDING` field remains. Verify every public link in a
signed-out browser.
