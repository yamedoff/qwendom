# Qwendom benchmark harness

This package contains the public task definitions, prompt builders, schemas, runners, and deterministic evaluators used to compare a single Qwen agent with the Qwendom society.

The public submission summary and current aggregate are documented in [`docs/BENCHMARK.md`](../../docs/BENCHMARK.md). Large provider-run bundles and internal reports are intentionally not committed.

## Design rules

1. **Same task, different operating model.** A comparison gives both modes the same mission, evidence packet, model family, output contract, and evaluator. The baseline answers alone; Qwendom may coordinate and iterate.
2. **No answer leakage.** Model-facing prompt builders import only public fixtures. Ground truth remains evaluator-only.
3. **Deterministic scoring.** Python checks structured output against the frozen answer key. A second model does not score the answer.
4. **Governance earns zero direct points.** Calls, agents, discussion, and votes matter only if they improve the scored output.
5. **Failures stay visible.** Timeouts, malformed outputs, incomplete usage, and failed attempts are recorded rather than silently converted to successful trials.
6. **Usage remains usage.** Missing token or cost metadata stays unknown; it is never converted to zero.

## Package map

| Area | Purpose |
|---|---|
| `fixtures/` | Public incident evidence and evaluator-only answer keys for the earlier suites |
| `loader.py`, `loader_v2.py`, `loader_v3.py` | Build model-facing prompts without importing private answers |
| `evaluator.py`, `evaluator_v2.py`, `evaluator_v3.py` | Deterministic scoring |
| `single_agent.py` | Run the baseline through the configured Qwen model |
| `society_adapter.py` | Adapt Qwendom output to the benchmark contract |
| `runtime.py` | Trial status, raw output, usage, and error schemas |
| `reporting.py` | Aggregate retained trials without hiding incomplete usage |
| `run_benchmark*.py` | Suite entry points |
| `outcome_v4/` | Development scenarios for artifact-producing and reliability evaluation |

## Evaluation boundary

The reference incident suites expose facts, candidate IDs, requirements, and constraints to the model. Private answer sets are imported only by evaluators and tests.

The scorer checks structured requirements such as:

- supported causes and evidence;
- required actions and order;
- safety controls;
- release constraints;
- requested numeric results; and
- invalid or unsupported identifiers.

Prose style is not a substitute for a required field or evidence record.

## Running evaluator tests

From the repository root:

```powershell
python -m pytest backend/tests/test_benchmark_evaluator.py -q
python -m pytest backend/tests/test_benchmark_single_agent.py -q
python -m pytest backend/tests/test_benchmark_reporting.py -q
```

Provider runs require the configured Qwen credentials. Deterministic evaluator tests do not call Qwen or AgentBay.

## Public-result discipline

The public repository keeps the methodology and evaluation code, not bulky run reports. A published aggregate must come from retained run artifacts and must state:

- task and suite version;
- number of trials and failed attempts;
- model and mode;
- quality and acceptance results;
- model calls and token usage; and
- what the comparison does and does not establish.

Do not mix provider results with deterministic no-key lifecycle runs or development smoke tests.
