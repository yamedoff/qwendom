# How the Qwendom benchmark works

<!-- BENCHMARK_STATUS: final -->

## The simple version

We give the same tool-enriched security release audit to:

1. one Qwen agent working alone; and
2. the Qwendom society, where several Qwen agents divide the work, review it,
   vote, and combine their answer.

Both sides use the same model: `qwen3.7-plus`.

We then compare:

- answer quality;
- time taken;
- number of model calls (requests sent to Qwen);
- tokens used (the pieces of text Qwen reads and writes); and
- failures.

The goal is not to prove that more agents are always better. The goal is to
find out whether the extra teamwork produces enough improvement to justify its
extra time and token use.

## What official version 3 tests

Version 3 is a composite security release audit. Both modes must:

- select seven supported release blockers while rejecting two safe decoys;
- order four containment, remediation, testing, and deployment stages;
- cite exact public evidence records for seven defined claim labels;
- satisfy three release constraints; and
- calculate three required numeric answers.

The harness retrieves the same four read-only evidence surfaces once for each
mode and records those calls. Agents may also use the same record lookup and
calculator tools. Both modes have the same 16-tool-call ceiling, 1.5M-token
ceiling, 900-second timeout, prompt, and `qwen3.7-plus` model. External research
is disabled.

The public task defines what every claim and numeric label means. Exact record
sets and values remain in a private key imported only by the deterministic
evaluator. Twenty-four binary acceptance checks determine quality; discussion
or extra events earn no points.

## Official version 3 result

Three successful trials were required per mode, and no attempt was discarded.

| Result | Single agent | Society |
|---|---:|---:|
| Successful trials | 3/3 | 3/3 |
| Failed attempts | 0 | 0 |
| Mean quality | 100.00 | 100.00 |
| Acceptance checks | 72/72 | 72/72 |
| Mean time per trial | 11.71 seconds | 233.17 seconds |
| Qwen calls | 3 | 53 |
| Tokens | 8,310 | 810,389 |
| Recorded evidence-tool calls | 12 | 28 |
| Checks per minute | 122.96 | 6.18 |
| Checks per million tokens | 8,664.26 | 88.85 |

Both modes reached the same perfect quality. Society was about **19.9 times
slower** and used about **97.5 times more tokens**. This is not a quality or
efficiency win. It proves that the society performs real, inspectable teamwork,
but the coordination cost is not justified on this task.

## What historical version 2 tests

Version 1 used only one incident. Version 2 keeps that kind of question and
adds three different asks:

1. diagnose an incident;
2. prioritize a plan under a deadline;
3. decide which claims are actually proven; and
4. choose the best option under cost and time limits.

Each ask publishes the exact claim labels the answer must contain. The correct
IDs and supporting facts live in a separate private answer-key module that the
prompt builder never imports.

Both modes may use the same three safe, read-only tools:

- look up one public record;
- look up several public records; and
- calculate basic arithmetic.

Tool calls are recorded. The tools cannot browse, run code, write files, or
read the private answers.

Each ask is scored out of 100. The final quality number is the average of the
four ask scores, with every ask weighted equally.

## Historical official version 2 result

We required three successful trials for every ask in both modes. Failed
attempts were retained.

| Result | Single agent | Society |
|---|---:|---:|
| Successful scored trials | 12 | 12 |
| Additional failed attempts | 1 | 0 |
| Equal-weight macro quality | 90.00 | 90.78 |
| Mean time per attempt | 13.15 seconds | 382.16 seconds |
| Qwen calls | 13 | 447 |
| Known tokens | 14,467* | 5,297,910 |
| Recorded tool calls | 0 | 96 |

\*The single agent's failed malformed-JSON attempt did not return complete
usage metadata, so 14,467 is the known total for its 12 successful attempts.

The society gained only **0.78 quality points**. It did better on incident
diagnosis, tied on planning and evidence verification, and did slightly worse
on the constraint task. It used roughly 34 times as many model calls and took
roughly 29 times longer per attempt. This is not a meaningful efficiency win.

## First optimization check

We ran the incident ask once before and once after replacing the repeated
all-member/all-proposal review matrix with one cross-review per member.
Everything else stayed fixed.

| Metric | Before | After |
|---|---:|---:|
| Quality score | 98 | 98 |
| Time | 477.52 seconds | 405.20 seconds |
| Qwen calls | 53 | 44 |
| Tokens | 913,777 | 661,155 |

This saved 9 calls, about 15% of the time, and about 28% of the tokens without
reducing quality in this one incident trial. One trial is useful engineering
evidence, but it is not enough for the final hackathon claim.

## What version 1 tested

Both sides receive the same fictional production incident.

The evidence includes 25 facts, five possible causes, five possible actions,
five possible safety controls, and three business constraints. Some facts are
useful and some are distractions.

Each side must return:

- the most likely cause;
- the fact IDs supporting that cause;
- the actions to take, in order;
- the safety controls to add;
- evidence for its important claims; and
- the business constraints it considered.

## What happens during one benchmark?

### 1. Prepare one identical prompt

The benchmark builds each task prompt once. The single agent and the society
receive byte-identical task text.

### 2. Run the single agent

One `qwen3.7-plus` agent reads the incident and returns one structured answer.

### 3. Run the society

Qwendom's agents discuss the task, divide work, make proposals, review each
other, vote, and produce a final structured answer.

The society receives no extra facts or outside research.

### 4. Score both answers with the same answer checker

A normal Python program scores the answers. A second AI model does not judge
them.

The 100 points cover:

| Area | Points |
|---|---:|
| Correct cause and supporting facts | 25 |
| Correct actions and order | 25 |
| Correct safety controls | 15 |
| Valid, useful evidence | 20 |
| Business constraints acknowledged | 15 |

The agents get no points merely for discussing, voting, or producing more
events. The final answer must actually be correct.

### 5. Repeat three times

We require three successful runs from each side before naming a winner. Failed
runs are kept rather than deleted.

Each run has the same maximum limits:

- 1,200 seconds;
- 700,000 tokens;
- no external research.

## Why the first result is provisional

The first version had an unfair scoring mistake.

Imagine a teacher asks for an explanation, but secretly awards ten points only
if the student uses three exact section titles. The student was never told
those titles. A correct explanation under different titles gets zero.

That is what happened here. The checker expected these exact labels:

- `deployment_cause`;
- `connection_exhaustion`;
- `cascading_failure`.

The prompt only asked agents to write a claim. It did not reveal those labels.
Both sides used sensible English labels and both lost the same ten points.

Treating both sides equally does not make that question fair: it measures
whether they guessed hidden labels, not whether their evidence was correct.

## What did version 1 show?

| Result | Single agent | Society |
|---|---:|---:|
| Successful runs | 3/3 | 3/3 |
| Average quality score | 86.25 | 87.64 |
| Average time | 17.90 seconds | 556.47 seconds |
| Model calls | 3 | 175 |
| Total tokens | 5,127 | 3,533,322 |
| Failed runs | 0 | 0 |

In plain language:

- The society scored only 1.39 points higher.
- That is too small to call it a clear quality win.
- The society took about 31 times longer.
- The society used about 689 times more tokens.

So version 1 does not prove that the society is better. It shows that the
current teamwork process is far too expensive.

These numbers are kept for transparency, but they are not final submission
numbers because of the hidden-label mistake.

## What we changed for version 2

We published the required claim labels, kept the correct supporting fact IDs
private, froze the repaired rules as version 2, reduced redundant coordination
calls, and ran three successful trials per ask and mode.

Version 1 and version 2 results must never be mixed.

## How to reproduce version 1

From `backend`, with `QWEN_API_KEY` configured:

```powershell
python -m benchmarks.run_benchmark `
  --successful-trials 3 `
  --max-attempts 5 `
  --timeout-seconds 900 `
  --token-budget 1500000 `
  --mode both `
  --output benchmark_results\official-2026-07-13-qwen37plus
```

The saved prompts, raw answers, scores, usage numbers, and comparison are in
[`backend/benchmark_results/official-2026-07-13-qwen37plus`](../backend/benchmark_results/official-2026-07-13-qwen37plus/).

## How to reproduce version 2

From `backend`, with `QWEN_API_KEY` configured:

```powershell
python -m benchmarks.run_benchmark_v2 `
  --successful-trials 3 `
  --max-attempts 4 `
  --timeout-seconds 1200 `
  --token-budget 700000 `
  --mode both `
  --task-concurrency 4 `
  --output benchmark_results\v2-official-3x-4c
```

The official v2 comparison and every raw attempt are in
[`backend/benchmark_results/v2-official-3x-4c`](../backend/benchmark_results/v2-official-3x-4c/).

## How to reproduce version 3

From `backend`, with `QWEN_API_KEY` configured:

```powershell
python -m benchmarks.run_benchmark_v3 `
  --successful-trials 3 `
  --max-attempts 5 `
  --timeout-seconds 900 `
  --token-budget 1500000 `
  --mode both `
  --output benchmark_results\v3-official-3x
```

The official v3 report, comparison, configuration, raw attempts, and isolated
runtime evidence are in
[`backend/benchmark_results/v3-official-3x`](../backend/benchmark_results/v3-official-3x/).

Future text and multimodal scenario ideas are preserved in
[`docs/BENCHMARK_SCENARIO_BACKLOG.md`](BENCHMARK_SCENARIO_BACKLOG.md). They do
not alter frozen suites v2 or v3.
