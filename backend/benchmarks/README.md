# Benchmark Slice 1 — Single-Agent vs Society Reference Task

## Purpose

A deterministic, no-sandbox, no-LLM evaluation contract for comparing
incident-response quality between single-agent and society modes on an
identical reference task.

## Hidden Ground-Truth Boundary

The ground-truth fixture (`fixtures/ground_truth.py`) is **never** imported
by the loader or prompt builder.  A model running the benchmark sees only
the facts, candidate IDs, and constraints from `fixtures/incident_packet.py`.
The evaluator and test suite are the only consumers of ground truth, ensuring
that no information leaks into the model-facing prompt.

## No-Sandbox Design

This benchmark requires no sandbox, no Docker container, and no LLM call.
The evaluator is a pure Python function that compares structured ID sets
against ground truth.  This makes it deterministic, fast, and trivially
reproducible across environments.

## Fairness Guarantees

| Guarantee | Mechanism |
|---|---|
| Identical rubric | `evaluator.evaluate()` accepts a `mode` label but never branches on it |
| No prompt leakage | `loader.build_prompt()` imports only from `fixtures.incident_packet`; ground truth is a separate module |
| Exact comparisons | All checks use ID equality, set Jaccard, or ordered-sequence match — never prose keywords |
| Evidence validation | Every cited fact ID is checked against the public `VALID_FACT_IDS` set |
| Unsupported-ID penalty | −3 pts per invalid/unsupported fact ID cited |
| Governance exclusion | `governance_score` is hardcoded to 0; governance fields are never read by the scorer |
| Score clamping | Final score is `max(0, min(100, raw − penalties))` |

## Fixture Separation

```
benchmarks/
├── fixtures/
│   ├── incident_packet.py   ← public; fed to the model prompt
│   └── ground_truth.py      ← private; imported only by evaluator + tests
├── loader.py                ← builds prompt from incident_packet only
├── evaluator.py             ← pure scorer; imports ground_truth
├── comparison.py            ← mode-neutral helpers
└── README.md
```

The ground-truth module is **never** imported by `loader.py`.  A model
running the benchmark sees only the facts, candidate IDs, and constraints
from `incident_packet.py`.

## Rubric (100 points)

| Category | Points | Method |
|---|---|---|
| Root cause — hypothesis ID | 15 | Exact string match |
| Root cause — evidence | 10 | F1 over required fact-ID set |
| Actions — set | 15 | Jaccard similarity |
| Actions — order | 10 | Positional match on filtered sequence |
| Controls — set | 15 | Jaccard similarity |
| Evidence — valid IDs | 5 | Ratio of valid to total cited |
| Evidence — required claims | 10 | Fraction of claims fully supported |
| Evidence — no unsupported | 5 | Binary: 0 if any invalid ID cited |
| Constraints — set | 15 | Jaccard similarity |
| **Penalty** | −3 each | Per invalid fact ID cited |
| **Governance** | 0 | Excluded from scoring |

## Running Tests

```bash
python -m pytest backend/tests/test_benchmark_evaluator.py -v
python -m pytest backend/tests/test_benchmark_single_agent.py -v
```

An official comparison requires at least three successful trials per mode.
One-run warm-ups are reported as `insufficient_data` and must not be presented
as a final winner. Provider cost remains unknown when DashScope omits pricing
metadata; the reporter never converts an unknown cost to zero.

## Slice 2A — Single-Agent Runner

### Purpose

An injectable, documented qwen3.7-plus single-agent runner for the
IncidentDecision benchmark.  The runner constructs an Agno Agent with
structured output, runs the reference prompt, parses the response, and
invokes the pure evaluator — all without touching production files.

### Files

| File | Role |
|---|---|
| `runtime.py` | `TrialUsage` (Agno field names) / `TrialResult` Pydantic models, safe `parse_decision`, `extract_metrics` |
| `single_agent.py` | `run_single_agent` async entry point — awaits `agent.arun(prompt)` with injectable `agent_factory` and `clock` |

### Design Decisions

* **Provider guard** — `run_single_agent` requires `settings.provider == 'qwen'`
  and `settings.qwen_model == 'qwen3.7-plus'` when no `agent_factory` is
  injected.  Tests bypass this guard by supplying a fake factory.
* **Async arun** — The runner awaits `agent.arun(prompt)` directly rather
  than wrapping a synchronous `Agent.run` in a thread.  Fake agents in tests
  implement `async def arun` for full coverage without provider calls.
* **Safe parser** — `parse_decision` accepts `IncidentDecision`, any
  `BaseModel`, `dict`, or a strict full-JSON `str`.  Embedded-prose mining
  is explicitly rejected.
* **Raw output serialization** — `raw_output` is always a `str | None`.
  When the response content is a `BaseModel` or `dict`, it is serialized to
  a stable JSON string (`sort_keys=True`).  When it is already a `str`, it
  is stored verbatim.  A `BaseModel` is never assigned into the `str` field.
* **Agno metrics** — `extract_metrics` reads `RunMetrics.to_dict()` and maps
  Agno field names: `input_tokens`, `output_tokens`, `total_tokens`
  (required for `usage_complete`), plus optional `cache_read_tokens`,
  `cache_write_tokens`, `reasoning_tokens`, `cost`, `duration`, and
  `time_to_first_token`.
* **No tools** — The agent instructions explicitly forbid tools and external
  evidence because the incident packet is self-contained.
* **Hard timeout** — `asyncio.wait_for` enforces a wall-clock deadline;
  both timeout and arbitrary exceptions are preserved in `TrialResult.error`.
* **Injectable dependencies** — `agent_factory` and `clock` are keyword-only
  parameters so tests never call a real provider.

### TrialResult Schema

| Field | Type | Description |
|---|---|---|
| `trial_id` | `str` | UUID |
| `mode` | `"single_agent"` | Always single_agent for this runner |
| `provider` | `str` | e.g. `"qwen"` |
| `model` | `str` | e.g. `"qwen3.7-plus"` |
| `task_hash` | `str` | SHA-256 of the task identifier |
| `prompt_hash` | `str` | SHA-256 of the rendered prompt |
| `started_at` / `finished_at` | `str` | ISO-8601 UTC timestamps |
| `wall_duration_s` | `float` | Elapsed seconds |
| `status` | `"success"` / `"failed"` | Outcome |
| `raw_output` | `str \| None` | Stable JSON string (BaseModel/dict → `json.dumps`) or verbatim string |
| `parsed_decision` | `IncidentDecision \| None` | Parsed structured output |
| `evaluation` | `EvaluationResult \| None` | Pure evaluator result |
| `usage` | `TrialUsage` | Agno token-level metrics (input/output/total + optional cache/reasoning/cost/duration/ttft) |
| `error` | `str \| None` | Preserved exception message |

### Incident Scenario

A payment-service deployment (v2.4.1) introduced a connection pooling
library that allocates 50 connections per pod (up from 10), exhausting the
database pool and causing cascading timeouts in the api-gateway.  The
packet contains 25 facts, 3 contradiction pairs, 2 red herrings, and 3
stakeholder constraints.
