# Agno-Native Capabilities: Activation Guide

This document describes how to turn the current Qwendom society from a visible
governance demo into a genuinely useful multi-agent system by leaning harder on
Agno-native primitives that already exist in the codebase or are directly
supported by the framework.

It complements [SOCIETY_RUNTIME.md](./SOCIETY_RUNTIME.md):

- `docs/SOCIETY_RUNTIME.md` explains what the current V3 runtime does.
- `docs/AGNO_NATIVE_CAPABILITIES.md` explains how to activate Agno-native
  capabilities so the society can use better tools, generate artifacts, route
  work more intelligently, and produce outputs that are verifiable and useful.

## Goal

The target is not "more agent theater." The target is a society that can:

1. choose the right specialists and tools for a task,
2. produce concrete artifacts instead of mostly conversational output,
3. validate those artifacts before accepting them,
4. retain useful memory and knowledge across tasks, and
5. preserve the existing UI/event contract so the demo surface stays stable.

## Current Gap

The current runtime already imports and uses important Agno primitives:

- `Agent`
- `Team`
- `Workflow`
- `RunContext`
- agentic memory flags
- session state
- knowledge injection
- native tools

But most of the task logic still lives in the orchestrator as hand-rolled
phase code. The result is a system that demonstrates collaboration patterns,
while leaving much of Agno's multi-agent runtime value underused.

Today, the biggest gaps are:

- tool use is mostly governance-oriented rather than task-capability-oriented,
- the team is used for one bounded coordination brief rather than sustained
  delegation,
- workflow checkpoints mostly record phase entry instead of doing substantive
  work,
- artifacts are implicit in text outputs rather than tracked explicitly, and
- several Agno-native tool modules are defined but not part of the active task
  path.

## What "Useful" Means Here

For Qwendom, usefulness should mean the society can take a task and return a
verified deliverable, not only a selected proposal.

Useful outputs include:

- a research brief,
- an implementation plan,
- a decision log,
- a subtask assignment ledger,
- a validation checklist,
- a critique report,
- a final answer with evidence and confidence,
- eventually a patch, report, or artifact bundle depending on the task type.

That pushes the society toward this operating model:

1. classify the task,
2. route it to the right specialists and tools,
3. generate typed artifacts,
4. validate and revise them,
5. synthesize a final deliverable,
6. preserve the useful lessons.

## Agno Primitives To Build On

The most important Agno-native primitives for this transition are:

### 1. `RunContext` and stateful tools

Current files:

- `backend/society/tools/governance.py`
- `backend/society/tools/capabilities.py`
- `backend/society/tools/debate.py`
- `backend/society/tools/voting.py`
- `backend/society/tools/delegation.py`
- `backend/society/tools/evaluation.py`

Why it matters:

- `RunContext.session_state` is the cleanest place to record proposals,
  ballots, subtasks, revisions, metrics, and artifacts.
- It lets tools mutate shared state at the moment work happens instead of
  forcing the orchestrator to simulate everything after the fact.

What to use it for next:

- proposal creation,
- challenge and revision,
- subtask assignment and reporting,
- artifact registration,
- evaluation scoring,
- final validation outcomes.

### 2. `Workflow`, `Step`, `Condition`, `Router`, and `Loop`

Current file:

- `backend/society/workflow.py`

Why it matters:

- The current workflow is a fixed phase spine.
- Agno supports workflows that branch, loop, and gate progression based on
  actual state or prior outputs.

What to use it for next:

- route research-heavy tasks to a research path,
- route build-heavy tasks to a builder/reviewer path,
- skip unnecessary phases,
- repeat debate or validation until quality is acceptable,
- stop early when consensus or sufficiency is reached.

### 3. `Team` with `TeamMode.coordinate`

Current file:

- `backend/society/team.py`

Why it matters:

- The current team is structurally correct but underused.
- Agno can coordinate delegation between members, which is closer to a useful
  society than a one-shot coordination brief.

What to use it for next:

- leader-driven delegation,
- member status reporting,
- subteam-style synthesis,
- passing shared context through session state instead of discarding team output.

### 4. Agentic memory and session summaries

Current file:

- `backend/society/agents.py`

Why it matters:

- The agent factory already enables memory-related features:
  `enable_agentic_memory=True`, `update_memory_on_run=True`,
  `add_memories_to_context=True`.
- This should become a real mechanism for useful task carry-over, not just a
  secondary feature next to the JSONL event log.

What to use it for next:

- remembering successful task patterns,
- recalling failed validation conditions,
- tracking reliable specialist behavior,
- storing concise reusable lessons instead of flat collaboration statements.

### 5. Knowledge injection

Current files:

- `backend/society/agents.py`
- `backend/society/knowledge/loaders.py`

Why it matters:

- Role-specific knowledge is the easiest way to make agents useful without
  making prompts huge.
- It is also the cleanest place for doctrine, checklists, templates, and
  domain-specific retrieval.

What to use it for next:

- architect doctrine,
- reviewer checklists,
- implementation templates,
- evidence standards,
- artifact-specific rubrics.

### 6. Structured tool outputs

Current files:

- `backend/society/schemas/capabilities.py`
- `backend/society/schemas/governance.py`
- `backend/society/schemas/evaluation.py`
- `backend/society/orchestrator.py`

Why it matters:

- The runtime already enforces explicit tool results in LLM mode.
- That is a strong base for artifact-first execution because tools can return
  typed objects instead of freeform prose.

What to use it for next:

- artifact records,
- validation results,
- acceptance criteria,
- routing decisions,
- per-step confidence.

## Current Agno Feature Inventory

| Primitive | Current usage | Status | Main file |
|---|---|---|---|
| `Agent` | role-based agents with model, memory, knowledge, tool settings | active | `backend/society/agents.py` |
| `Team` | one bounded coordination pass in LLM mode | partially active | `backend/society/team.py`, `backend/society/orchestrator.py` |
| `Workflow` | fixed six-step lifecycle spine | active but thin | `backend/society/workflow.py` |
| `RunContext` | state mutation in tools | active | `backend/society/tools/*.py` |
| Agentic memory | enabled on agents | partially active | `backend/society/agents.py` |
| Knowledge | loaded by role when available | partially active | `backend/society/agents.py`, `backend/society/knowledge/loaders.py` |
| Structured schemas | enforced for governance/capability extraction | active | `backend/society/schemas/*.py` |
| Debate tools | defined but not active path | dormant | `backend/society/tools/debate.py` |
| Voting tools | defined but not active path | dormant | `backend/society/tools/voting.py` |
| Delegation tools | defined but not active path | dormant | `backend/society/tools/delegation.py` |
| Evaluation tool | defined but not active path | dormant | `backend/society/tools/evaluation.py` |

## Dormant Tool Modules To Activate

### Debate tools

File:

- `backend/society/tools/debate.py`

Current gap:

- `propose`, `challenge`, and `revise` are defined as Agno tools that mutate
  shared session state.
- The active runtime still constructs proposals, challenges, and revisions
  mostly in orchestrator code.

Why activation matters:

- It moves debate from "simulated collaboration" toward actual tool-mediated
  collaboration.
- It also makes debate artifacts explicit in session state.

Activation target:

- let members call `propose`,
- let challengers call `challenge`,
- let targets call `revise`,
- map those tool actions to the existing event types the frontend already
  expects.

### Voting tools

File:

- `backend/society/tools/voting.py`

Current gap:

- `cast_ballot` and `tally_ballots` already implement a stateful ballot path,
  but the active runtime uses `governance.cast_vote` plus direct orchestrator
  tallying.

Why activation matters:

- It makes voting a native stateful process rather than a mixed
  tool-and-orchestrator path.
- It simplifies replay because ballots and tally become first-class tool
  outputs.

Activation target:

- use `cast_ballot` for each member,
- use `tally_ballots` to finalize the winner,
- preserve the current `vote_cast`, `ballots_tallied`, and
  `solution_selected` events.

### Delegation tools

File:

- `backend/society/tools/delegation.py`

Current gap:

- child agents can spawn, but there is no true delegation lifecycle.
- `assign_subtask` and `report_subtask` are not wired into the active task run.

Why activation matters:

- This is the key shift from "agents with opinions" to "agents doing work."
- It gives the leader and spawned specialists something concrete to coordinate.

Activation target:

- leader assigns subtasks,
- members or child agents report results and blockers,
- the synthesis phase consumes those reports,
- the final answer cites completed subtasks and unresolved blockers.

### Evaluation tool

File:

- `backend/society/tools/evaluation.py`

Current gap:

- `record_metric` writes to `session_state["evaluation_metrics"]`, but the
  runtime primarily computes metrics at the end through
  `compute_task_metrics()`.

Why activation matters:

- It creates a path for agent-driven evaluation, not just system-side
  accounting.
- This is important if the society is meant to judge artifact quality, not
  only count lifecycle events.

Activation target:

- record artifact quality scores,
- record validation outcomes,
- record confidence and completion quality,
- fold those into final metrics and replay.

## The Biggest Architectural Shift: Artifact-First Execution

The society becomes useful when each significant phase emits a typed artifact.

Recommended additions to session state:

```json
{
  "artifacts": [],
  "artifact_index": {},
  "acceptance_checks": [],
  "failed_checks": [],
  "final_deliverable": null
}
```

These fields should be additive to the current `initial_session_state()` shape,
not a replacement for it. Existing fields such as `proposals`, `challenges`,
`revisions`, `ballots`, and `tally` should continue to exist during migration,
with artifacts initially acting as a parallel typed ledger for those events.
Only after the artifact model is stable should any older state fields be
collapsed or treated as derived views.

Recommended artifact types:

- `research_brief`
- `implementation_plan`
- `risk_report`
- `proposal`
- `revision`
- `ballot_tally`
- `subtask_report`
- `validation_report`
- `final_answer`

Recommended artifact fields:

- `id`
- `type`
- `producer`
- `phase`
- `content`
- `depends_on`
- `confidence`
- `created_at`

Recommended implementation detail:

- define a dedicated artifact schema module, for example
  `backend/society/schemas/artifacts.py`,
- store typed artifact records in session state as plain JSON-serializable
  dicts,
- keep `artifact_index` as an id-to-record lookup cache, not a second source of
  truth.

Example artifact record:

```json
{
  "id": "artifact-proposal-architect-1",
  "type": "proposal",
  "producer": "architect",
  "phase": "debate",
  "content": {
    "proposal": "Start with a scoped implementation plan and a validation gate.",
    "rationale": "This creates a deliverable the team can vote on."
  },
  "depends_on": [],
  "confidence": 0.82,
  "created_at": "2026-06-26T20:00:00Z"
}
```

What changes operationally:

- tools write artifacts,
- workflow steps validate artifacts,
- later steps consume artifact ids instead of rewriting prior reasoning in
  prose,
- the final answer can cite which artifacts it used.

## Tool Access That Makes The Society Useful

The current society has enough governance tooling to demonstrate structure, but
it needs more task-facing tools to become useful.

The capability direction should be:

- researcher: retrieval, evidence gathering, knowledge search, context
  extraction,
- architect: decomposition, task routing, interface planning, acceptance
  criteria drafting,
- builder: implementation planning, artifact assembly, integration summaries,
- critic: validation, contradiction detection, quality gates, risk scoring,
- leader/coordinator: delegation, artifact handoff, completion checks.

That does not require every tool to exist immediately. It does require the doc
and the implementation plan to stop treating all useful work as plain text.

## Workflow Evolution

### Current state

The current workflow records a six-step lifecycle:

1. `form_and_elect`
2. `spawn_or_delegate`
3. `debate`
4. `vote`
5. `monitor`
6. `learn_and_measure`

This is a good spine, but the steps mostly mark progression rather than doing
substantive task work.

Before using `Router`, `Condition`, or `Loop`, verify that the installed Agno
version in this repo exposes the workflow primitives intended for use. The
design should follow Agno's documented workflow patterns, but implementation
must stay constrained to primitives actually available in the installed SDK.

### Target state

Keep the same conceptual phases, but make them operational:

1. `form_and_elect`
   - classify task,
   - select roster,
   - choose leader,
   - initialize artifacts and acceptance checks.

2. `spawn_or_delegate`
   - decide whether a child specialist is required,
   - assign subtasks,
   - record delegation artifacts.

3. `debate`
   - generate proposals,
   - challenge weak proposals,
   - revise proposals,
   - stop early if one proposal clearly satisfies acceptance checks.

4. `vote`
   - cast ballots,
   - tally ballots,
   - record consensus quality and dissent.

5. `monitor`
   - validate the selected plan or artifact,
   - record risks, blockers, and required corrections.

6. `learn_and_measure`
   - record reusable memory,
   - store evaluation artifacts,
   - update reputation,
   - compose the final deliverable bundle.

### Agno-native controls to add

- `Router`
  - choose different workflows for different task classes.

- `Condition`
  - skip debate or extra validation when not needed.

- `Loop`
  - repeat revision until a validation threshold is met or the iteration budget
    is exhausted.

## Team Coordination Evolution

### Current state

The Agno team is used for one bounded coordination pass and its output is
emitted as a brief.

### Target state

The team should become a real coordinator for artifact production:

- the leader assigns work,
- members contribute typed outputs,
- child agents handle subtasks,
- the leader synthesizes from artifact state instead of from unstructured chat.

Recommended changes:

- persist coordination output into session state,
- feed that output into negotiation and delegation phases,
- let team coordination set up the first-pass subtask plan,
- use team coordination to decide when the society needs more evidence versus
  more implementation detail.

Important constraint:

- the orchestrator should remain the authoritative event-emission layer even if
  the team performs more of the underlying coordination work.
- expanding team coordination does not mean giving up the current UI/replay
  contract; it means shifting more state production into Agno while keeping
  event mapping explicit in the orchestrator.

## Memory and Knowledge Evolution

### Memory

Current state:

- Agno memory flags are enabled,
- the event store persists memory-write events,
- the orchestrator also manually appends simple collaboration statements.

Target state:

- memory stores useful lessons, not only participation traces.

Mechanically, this should be done by improving the content written through the
existing memory pathways first, not by replacing Agno memory up front. The
first migration step should change what gets remembered and summarized; only
after that proves insufficient should memory storage architecture change.

Examples of useful memory:

- which acceptance checks were most predictive,
- which specialist roles improved outcomes,
- recurring failure patterns,
- successful artifact templates,
- domains where the critic correctly flagged hidden risk.

### Knowledge

Current state:

- role knowledge is optional and filesystem-backed.

Target state:

- knowledge folders should contain operational doctrine, templates, and
  checklists by role.

Examples:

- architect: decomposition patterns, interface templates,
- researcher: evidence hierarchy, source-quality checklist,
- builder: artifact templates, implementation plan shapes,
- critic: validation rubric, common failure modes, completion checklist.

## Deterministic Mode Must Stay First-Class

Any Agno-native expansion must preserve the current deterministic no-key mode.

That means:

- every active tool path needs a deterministic fallback,
- event names must remain stable,
- session state shape must remain JSON-serializable,
- the UI replay path must still work without LLM-backed tool runs.

Recommended rule:

- if an Agno-native tool is introduced into the active path, define the
  deterministic equivalent before making it authoritative.

Recommended deterministic artifact rule:

- every artifact type introduced in LLM mode should have a deterministic
  constructor that can emit the same shape with lower-fidelity content.
- deterministic mode does not need identical quality, but it does need the same
  artifact categories and the same event lifecycle.

## Event Contract To Preserve

The frontend and replay path rely on stable event names emitted by the
orchestrator.

When activating more native Agno behavior, keep these event families stable:

- `task_received`
- `team_formed`
- `workflow_checkpoint`
- `workflow_completed`
- `agno_team_ran`
- `leader_elected`
- `child_agent_spawned`
- `no_spawn`
- `agent_negotiated`
- `proposal_challenged`
- `proposal_revised`
- `debate_round_completed`
- `vote_cast`
- `ballots_tallied`
- `solution_selected`
- `peer_monitor_report`
- `negotiation_closed`
- `learning_recorded`
- `reputation_updated`
- `team_dissolved`
- `task_failed`
- `task_metrics`
- `task_complete`

The implementation should allow internal logic to become more Agno-native while
keeping the event surface legible and stable.

Recommended artifact event policy:

- do not add new frontend-critical event types until the existing event contract
  is preserved through the migration.
- artifact creation can initially ride inside existing payloads, or be emitted
  as optional additive events that the frontend can ignore safely.

## File-By-File Activation Map

| File | Current role | Activation priority | Main change |
|---|---|---|---|
| `backend/society/orchestrator.py` | owns most task logic and event emission | P0 | reduce hand-rolled governance, become coordinator and event mapper |
| `backend/society/workflow.py` | lifecycle spine and metrics | P0 | add meaningful work, conditions, and loops |
| `backend/society/session.py` | shared ledger schema | P0 | add artifacts, validation state, deliverable state |
| `backend/society/team.py` | Agno team factory | P1 | let the team coordinate delegation and artifact production |
| `backend/society/agents.py` | agent factory | P1 | make better use of memory, knowledge, and role-scoped toolsets |
| `backend/society/tools/debate.py` | dormant native debate primitives | P0 | make active path for proposal/challenge/revise |
| `backend/society/tools/voting.py` | dormant native voting primitives | P1 | become active ballot/tally path |
| `backend/society/tools/delegation.py` | dormant delegation primitives | P1 | wire leader/child subtask loop |
| `backend/society/tools/evaluation.py` | dormant evaluation primitive | P1 | record artifact quality and validation outcomes |
| `backend/society/tools/capabilities.py` | current role tools | P1 | evolve outputs toward artifact records and acceptance checks |
| `backend/society/reputation.py` | in-process reputation scoring | P2 | make reputation sensitive to actual artifact quality and validation |
| `backend/society/memory.py` | JSONL replay store | P1 | preserve artifact/evaluation events for replay |

Schema additions expected:

| File | Purpose |
|---|---|
| `backend/society/schemas/artifacts.py` | typed artifact record and artifact bundle schemas |
| `backend/society/schemas/validation.py` | validation result, acceptance check, and failed-check schemas if artifact validation becomes explicit |

## Recommended Rollout

### Phase 0: Make the current society output structured artifacts

Do first:

- extend session state with artifact fields,
- make proposals and critiques register as artifacts,
- make final answer reference produced artifacts.

Why:

- this is the shortest path from governance theater to useful output traceability.

Phase 0 acceptance criteria:

- session state can hold artifacts without breaking existing task runs,
- proposals and critiques can be represented both in legacy fields and in
  artifact form,
- final answers can cite artifact ids or artifact summaries without changing the
  SSE contract.

### Phase 1: Activate delegation and validation

Do next:

- wire `assign_subtask` and `report_subtask`,
- add validation artifacts and failed-check tracking,
- use critique as a real gate rather than a post-hoc comment.

Why:

- this makes the society do work and verify it.

Phase 1 acceptance criteria:

- leader-assigned subtasks transition cleanly through planned, assigned,
  completed, and blocked states,
- child-agent results are visible in session state and final synthesis,
- validation failures can halt progression or force revision in a predictable
  way.

### Phase 2: Move debate and voting fully into native tools

Do next:

- activate `propose`, `challenge`, `revise`,
- activate `cast_ballot` and `tally_ballots`,
- keep existing event emission as the compatibility layer,
- keep `governance.cast_vote` available until the `VoteDecision` extraction path
  is either preserved through the new ballot flow or intentionally replaced.

Why:

- this aligns task flow with Agno-native state mutation and makes replay
  cleaner.

Recommended rollout guard:

- add a feature flag or orchestrator switch so the active path can move between
  legacy vote handling and native ballot handling without breaking the UI.

### Phase 3: Add workflow routing and loops

Do next:

- add `Router` for task-class routing,
- add `Condition` for optional phases,
- add `Loop` for validation and revision.

Why:

- this is where the society starts adapting instead of replaying the same
  linear choreography every time.

Initial task classes should stay simple and explicit:

- research-dominant,
- planning-dominant,
- implementation-dominant,
- review-dominant.

The first router should classify into only a small number of stable paths so
the system remains debuggable.

### Phase 4: Strengthen memory, knowledge, and evaluation

Do next:

- replace flat manual memory writes with reusable lessons,
- populate role knowledge folders with useful doctrine,
- record agent-driven evaluation metrics,
- connect reputation to useful outcomes instead of only participation.

Why:

- this is the step that makes the society improve over time.

## Risks and Constraints

- The strict tool-result extraction path in `backend/society/orchestrator.py`
  should be preserved unless the validation model changes.
- Debate tools currently require an actual `RunContext`; they cannot simply be
  called as plain helper functions.
- Several dormant tools use `stop_after_tool_call=True`, which means multi-step
  flows require either one-tool-per-run orchestration or a deliberate redesign
  of how tool chaining is handled.
- Subtask planning currently has overlapping writers in `session_state`:
  planned subtasks and assigned subtasks need a clear lifecycle.
- Child-agent skill derivation is currently string-based, so specialist routing
  can become inconsistent unless capability mapping is made explicit.
- Reputation remains partly in-process today; if it becomes more important,
  persistence should be improved.

Failure semantics that should be documented in implementation work:

- if a tool call fails, record the failure into state and emit the matching
  event before retry or fallback,
- if a validation loop exceeds its iteration budget, emit a bounded failure or
  degraded completion state rather than silently continuing,
- if team coordination fails, fall back to orchestrator-driven continuation
  rather than aborting the whole task by default.

## Validation Checklist

Any implementation work derived from this document should validate:

```powershell
python -m compileall backend
cd backend; python -m society.preflight
cd frontend; npm run build
```

Behavioral validation should also include:

- one deterministic no-key task run,
- one LLM-backed task run,
- one task that spawns a child agent,
- one task that exercises delegation,
- one task that exercises validation and revision loops,
- verification that the SSE timeline still renders correctly in the frontend.

## Recommended Immediate Build Order

If only a small slice can be implemented next, the highest-value order is:

1. add artifact tracking to session state,
2. wire delegation tools into spawned-child workflows,
3. persist team coordination output instead of treating it as a disposable
   brief,
4. activate debate tools for native proposal/challenge/revise,
5. add validation loops before expanding reputation or social complexity.

That sequence keeps the society legible, preserves the current demo surface,
and moves the system toward useful multi-agent work rather than richer
simulation alone.
