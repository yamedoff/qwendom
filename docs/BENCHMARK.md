# Qwendom benchmark

<!-- BENCHMARK_STATUS: final -->

## Question

Can a Qwen agent society outperform one Qwen agent when the task rewards completeness and gives the society room to debate, block weak work, revise, and validate independently?

The benchmark compares:

1. a single Qwen agent producing the answer alone; and
2. Qwendom running the same mission through its society workflow.

Both modes receive the same security-release mission, evidence packet, Qwen model, and deterministic evaluator. The benchmark changes the operating structure, not the task: the baseline answers directly, while Qwendom can distribute work, register blockers, iterate after failure, and require independent validation.

## Task and scoring

The task is a composite security-release audit. Each trial must:

- identify supported release blockers and reject safe decoys;
- order containment, remediation, testing, and deployment actions;
- connect important claims to the supplied evidence;
- satisfy the release constraints; and
- return the requested calculations and structured fields.

The scorer is deterministic Python, not another language model. Discussion volume, agent count, and polished prose earn no points. Across three trials, the evaluator records 72 binary acceptance checks in total.

## Results

| Result across three trials | Single Qwen agent | Qwendom society |
|---|---:|---:|
| Mean quality | 42 | **100** |
| Acceptance checks passed | 47/72 | **72/72** |
| Total Qwen calls | 14 | 124 |
| Total tokens | 921K | 3.9M |

Qwendom closes every acceptance check in the comparison. The single agent completes 47 of 72 checks and receives a mean quality score of 42; Qwendom completes 72 of 72 and scores 100.

The extra cost is visible. Qwendom uses more calls and tokens because it does more than produce a first answer:

1. specialists state and challenge positions;
2. critical objections become blockers instead of footnotes;
3. the leader assigns bounded work to capability-matched employees;
4. failed checks return the artifact to the producer;
5. revisions are exported with new evidence; and
6. a separate specialist reruns the acceptance path.

**Qwendom wins this benchmark because it can iterate until the evidence, artifact, and independent verdict agree.** The benchmark does not show that a society is cheaper. It shows that additional coordination can be worth its cost when incomplete work is the dominant risk.

## Fairness boundary

The comparison holds constant:

- the mission and public evidence;
- the Qwen model family;
- the required output contract;
- the deterministic evaluator; and
- the acceptance criteria.

The comparison intentionally does not hold the orchestration loop constant. Iteration, specialist separation, blockers, and independent validation are the product capabilities being evaluated.

## Evidence and repository scope

The repository contains the benchmark task definitions, prompt loaders, schemas, and deterministic evaluators under `backend/benchmarks/`. Large raw provider run bundles and internal benchmark reports are not committed to the public repository. The figures above summarize the retained special benchmark run used for the submission.

That boundary matters:

- public code documents how tasks are constructed and scored;
- retained run artifacts support the published aggregate;
- no conversation volume or governance event is counted as answer quality; and
- benchmark results are not mixed with deterministic no-key lifecycle tests.

## Interpreting the result

Use the single-agent path when speed and minimum inference cost dominate. Use Qwendom when the task benefits from ownership, adversarial review, execution evidence, and the ability to stop and repair incomplete work.

This result is specific to the benchmark above. It is not a universal claim that more agents always win.
